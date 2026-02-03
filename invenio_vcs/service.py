# -*- coding: utf-8 -*-
# This file is part of Invenio.
# Copyright (C) 2025-2026 CERN.
#
# Invenio is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.
"""Higher-level operations for the view handlers and upstream code to use."""

from dataclasses import asdict
from typing import TYPE_CHECKING

from flask import current_app
from invenio_accounts.models import User, UserIdentity
from invenio_db import db
from invenio_i18n import gettext as _
from invenio_oauth2server.models import Token as ProviderToken
from invenio_oauthclient import oauth_link_external_id
from sqlalchemy import delete
from werkzeug.utils import cached_property

from invenio_vcs.config import get_provider_by_id
from invenio_vcs.errors import (
    RemoteAccountDataNotSet,
    RemoteAccountNotFound,
    RepositoryAccessError,
    RepositoryNotFoundError,
    UserInfoNoneError,
)
from invenio_vcs.generic_models import GenericRepository
from invenio_vcs.models import (
    Release,
    ReleaseStatus,
    Repository,
    repository_user_association,
)
from invenio_vcs.proxies import current_vcs
from invenio_vcs.tasks import sync_hooks as sync_hooks_task
from invenio_vcs.tasks import sync_repo_users as sync_repo_users_task
from invenio_vcs.utils import iso_utcnow

if TYPE_CHECKING:
    from invenio_vcs.providers import (
        RepositoryServiceProvider,
    )


class VCSService:
    """
    High level glue operations that operate on both the VCS and the DB.

    Because provider instances are user-specific, this class is too.
    """

    def __init__(self, provider: "RepositoryServiceProvider") -> None:
        """Please construct the service using the `for_provider_and_user` method instead."""
        self.provider = provider

    @staticmethod
    def for_provider_and_user(provider_id: str, user_id: int):
        """Construct VCSService for a locally configured provider and a user with a DB-queried access token."""
        return VCSService(get_provider_by_id(provider_id).for_user(user_id))

    @staticmethod
    def for_provider_and_token(provider_id: str, user_id: int, access_token: str):
        """Construct VCSService for a locally configured provider and a user with a predefined access token."""
        return VCSService(
            get_provider_by_id(provider_id).for_access_token(user_id, access_token)
        )

    @cached_property
    def is_authenticated(self):
        """Whether we have a valid VCS API token for the user. Should (almost) always return True."""
        return self.provider.oauth_remote_token is not None

    @property
    def user_available_repositories(self):
        """Retrieve user repositories from user's remote data."""
        return Repository.query.join(repository_user_association).filter(
            repository_user_association.c.user_id == self.provider.user_id,
            Repository.provider == self.provider.factory.id,
        )

    @property
    def user_enabled_repositories(self):
        """Retrieve user repositories from the model."""
        return Repository.query.join(repository_user_association).filter(
            repository_user_association.c.user_id == self.provider.user_id,
            Repository.provider == self.provider.factory.id,
            Repository.hook != None,
        )

    def list_repositories(self):
        """Retrieves user repositories, containing db repositories plus remote repositories."""
        repos = {}
        for db_repo in self.user_available_repositories:
            repos[db_repo.provider_id] = asdict(GenericRepository.from_model(db_repo))
            release_instance = current_vcs.release_api_class(
                db_repo.latest_release(), self.provider
            )
            repos[db_repo.provider_id]["instance"] = db_repo
            repos[db_repo.provider_id]["latest"] = release_instance

        return repos

    def get_repo_latest_release(self, db_repo_id: str):
        """Retrieves the repository last release."""
        db_release = (
            Release.query.filter_by(
                repository_id=db_repo_id, status=ReleaseStatus.PUBLISHED
            )
            .order_by(db.desc(Release.created))
            .first()
        )
        if db_release is None:
            return None
        return current_vcs.release_api_class(db_release, self.provider)

    def list_repo_releases(self, repo):
        """Retrieve releases and sort them by descending creation date."""
        release_instances = []
        for release_object in repo.releases.order_by(db.desc(Release.created)):
            release_instances.append(
                current_vcs.release_api_class(release_object, self.provider)
            )
        return release_instances

    def get_repo_default_branch(self, vcs_repo_id):
        """Return the locally-synced default branch."""
        db_repo = self.user_available_repositories.filter(
            Repository.provider_id == vcs_repo_id
        ).one_or_none()

        if db_repo is None:
            return None

        return db_repo.default_branch

    def get_last_sync_time(self):
        """Retrieves the last sync delta time from VCS's client extra data.

        Time is computed as the delta between now and the last sync time.
        """
        extra_data = self.provider.oauth_remote_account.extra_data
        if not extra_data.get("last_sync"):
            raise RemoteAccountDataNotSet(
                self.provider.user_id,
                _("Last sync data is not set for user (remote data)."),
            )

        return extra_data["last_sync"]

    def get_repository(self, repo_id):
        """Retrieves one repository.

        Checks for access permission.
        """
        db_repo = Repository.get(self.provider.factory.id, provider_id=repo_id)
        if not db_repo:
            raise RepositoryNotFoundError(repo_id)

        # Might raise a RepositoryAccessError
        self.check_repo_access_permissions(db_repo)

        return db_repo

    def check_repo_access_permissions(self, db_repo: Repository):
        """Checks permissions from user on repo.

        The user has access to the repo if a row with the user's ID and the repo's ID
        is included in the repository-user association table, which is available via
        `self.user_available_repositories`.
        """

        user_has_remote_access_count = self.user_available_repositories.filter(
            Repository.provider_id == db_repo.provider_id
        ).count()
        if user_has_remote_access_count == 1:
            return True

        raise RepositoryAccessError(
            user=self.provider.user_id,
            repo=db_repo.full_name,
            repo_id=db_repo.provider_id,
        )

    def sync(self, hooks=True):
        """Synchronize user repositories.

        :param bool hooks: True for syncing hooks.

        .. note::

            Syncing happens from the VCS' direction only. This means that we
            consider the information on VCS as valid, and we overwrite our
            own state based on this information.
        """
        vcs_repos = self.provider.list_repositories()
        if vcs_repos is None:
            vcs_repos = {}

        # Get the list of repos the user currently has access to in the DB
        db_repos = (
            Repository.query.join(repository_user_association)
            .filter(
                repository_user_association.c.user_id == self.provider.user_id,
                Repository.provider == self.provider.factory.id,
            )
            .all()
        )
        # Update the DB repos with any new data from the VCS repos
        for db_repo in db_repos:
            vcs_repo = vcs_repos.get(db_repo.provider_id)
            if not vcs_repo:
                continue
            vcs_repo.update_model(db_repo)

        # Remove ownership from repositories that the user has no longer
        # access to or have been deleted.
        delete_stmt = delete(repository_user_association).where(
            repository_user_association.c.user_id == self.provider.user_id,
            Repository.provider == self.provider.factory.id,
            ~Repository.provider_id.in_(vcs_repos.keys()),
            repository_user_association.c.repository_id == Repository.id,
        )
        db.session.execute(delete_stmt)

        # Add new repos from VCS to the DB (without the hook activated)
        for _, vcs_repo in vcs_repos.items():
            # We cannot just check the repo from the existing `db_repos` list as this only includes the repos to which the user
            # already has access. E.g. a repo from the VCS might already exist in our DB but the user doesn't yet have access to it.
            corresponding_db_repo = Repository.query.filter(
                Repository.provider_id == vcs_repo.id,
                Repository.provider == self.provider.factory.id,
            ).first()

            if corresponding_db_repo is None:
                # We do not yet have this repo registered for any user at all in our DB, so we need to create it.
                corresponding_db_repo = Repository.create(
                    provider=self.provider.factory.id,
                    provider_id=vcs_repo.id,
                    default_branch=vcs_repo.default_branch,
                    full_name=vcs_repo.full_name,
                    description=vcs_repo.description,
                )

                # We need to flush to generate the ID for the repo, otherwise adding the user relation will fail.
                db.session.flush()
                # Add the user that triggered the sync now to avoid making them wait for the async tasks.
                corresponding_db_repo.add_user(self.provider.user_id)

        # Update last sync
        self.provider.oauth_remote_account.extra_data.update(
            dict(
                last_sync=iso_utcnow(),
            )
        )
        self.provider.oauth_remote_account.extra_data.changed()
        db.session.add(self.provider.oauth_remote_account)

        # Hooks and user sync will run asynchronously, so we need to commit any changes done so far
        db.session.commit()

        repo_provider_ids = list(vcs_repos.keys())
        if hooks:
            self._sync_hooks(repo_provider_ids)
        self._sync_repo_users(repo_provider_ids)

    def _sync_repo_users(self, repo_provider_ids: list[str]):
        """Start the batched sync of repo users."""
        batch_size = current_app.config["VCS_SYNC_BATCH_SIZE"]
        for i in range(0, len(repo_provider_ids), batch_size):
            sync_repo_users_task.delay(
                self.provider.factory.id,
                self.provider.user_id,
                repo_provider_ids[i : i + batch_size],
            )

    def sync_repo_users(self, repo_provider_id: str):
        """
        Synchronises the member users of the repository.

        This retrieves a list of the IDs of users from the VCS who have sufficient access to the
        repository (i.e. being able to access all details and create/manage webhooks).
        The user IDs are compared locally to find Invenio users who have connected their VCS account.
        This is then propagated to the database: Invenio users who have access to the repo are added to
        the `repository_user_association` table, and ones who no longer have access are removed.

        :return: boolean of whether any changed were made to the DB
        """
        db_repo = Repository.get(self.provider.factory.id, provider_id=repo_provider_id)
        if not db_repo:
            # This method is always called after the main sync, so we
            # expect `repo_provider_id` to exist already.
            raise RepositoryNotFoundError(repo_provider_id)

        vcs_user_ids = self.provider.list_repository_user_ids(db_repo.provider_id)
        if vcs_user_ids is None:
            return

        vcs_user_identities: list[UserIdentity] = []
        # Find local users who have connected their VCS accounts with the IDs from the repo members
        for extern_user_id in vcs_user_ids:
            user_identity = UserIdentity.query.filter_by(
                method=self.provider.factory.id,
                id=extern_user_id,
            ).first()

            if user_identity is None:
                continue

            vcs_user_identities.append(user_identity)

        # Create user associations that exist in the VCS but not in the DB
        for user_identity in vcs_user_identities:
            if not any(
                db_user.id == user_identity.id_user for db_user in db_repo.users
            ):
                db_repo.add_user(user_identity.id_user)

        # Remove user associations that exist in the DB but not in the VCS
        for db_user in db_repo.users:
            if not any(
                user_identity.id_user == db_user.id
                for user_identity in vcs_user_identities
            ):
                db_repo.remove_user(db_user.id)

    def _sync_hooks(self, repo_provider_ids: list[str]):
        """Start batched sync of hooks."""
        batch_size = current_app.config["VCS_SYNC_BATCH_SIZE"]
        for i in range(0, len(repo_provider_ids), batch_size):
            sync_hooks_task.delay(
                self.provider.factory.id,
                self.provider.user_id,
                repo_provider_ids[i : i + batch_size],
            )

    def sync_repo_hook(self, repo_id: str):
        """Sync a VCS repo's hook with the locally stored repo.

        The repository referred to by `repo_id` must already exist.
        """
        # Get the hook that we may have set in the past
        hook = self.provider.get_configured_webhook(repo_id)

        # If hook on the VCS exists, get or create corresponding db object and
        # enable the hook. Otherwise remove the old hook information.
        db_repo = Repository.get(self.provider.factory.id, provider_id=repo_id)

        if not db_repo:
            # This method is always called after the main sync, so we
            # expect `repo_id` to exist already.
            raise RepositoryNotFoundError(repo_id)

        if hook and not db_repo.enabled:
            self.mark_repo_enabled(db_repo, hook.id)
        elif hook is None and db_repo.enabled:
            self.mark_repo_disabled(db_repo)

    def mark_repo_disabled(self, db_repo: Repository):
        """Marks a repository as disabled."""
        db_repo.hook = None
        db_repo.enabled_by_user_id = None
        db_repo.record_community_id = None

    def mark_repo_enabled(
        self, db_repo: Repository, hook_id: str, community_id: str | None = None
    ):
        """Marks a repository as enabled."""
        db_repo.hook = hook_id
        db_repo.enabled_by_user_id = self.provider.user_id
        if community_id is not None:
            db_repo.record_community_id = community_id

    def init_account(self):
        """Setup a new VCS account."""
        if not self.provider.oauth_remote_account:
            raise RemoteAccountNotFound(
                self.provider.user_id, _("Remote account was not found for user.")
            )

        user = self.provider.get_own_user()
        if user is None:
            raise UserInfoNoneError

        # Setup local access tokens to be used by the webhooks
        hook_token = ProviderToken.create_personal(
            f"{self.provider.factory.id}-webhook",
            self.provider.user_id,
            scopes=["webhooks:event"],
            is_internal=True,
        )
        # Initial structure of extra data
        self.provider.oauth_remote_account.extra_data = dict(
            id=user.id,
            login=user.username,
            name=user.display_name,
            tokens=dict(
                webhook=hook_token.id,
            ),
            last_sync=iso_utcnow(),
        )

        oauth_link_external_id(
            User(id=self.provider.user_id),
            dict(id=user.id, method=self.provider.factory.id),
        )

        db.session.add(self.provider.oauth_remote_account)

    def enable_repository(self, provider_id: str, community_id: str | None = None):
        """Creates the hook for a repository and marks it as enabled."""
        db_repo = self.user_available_repositories.filter(
            Repository.provider_id == provider_id
        ).first()
        if db_repo is None:
            raise RepositoryNotFoundError(provider_id)

        # No further access check needed: the repo was already in the user's available repo list.

        existing_hook = self.provider.get_configured_webhook(provider_id)
        if existing_hook is None:
            hook_id = self.provider.create_webhook(provider_id)
            if hook_id is None:
                return False
        else:
            # Update the webhook if it already exists.
            hook_id = existing_hook.id
            if not self.provider.update_webhook(provider_id, hook_id):
                return False

        self.mark_repo_enabled(db_repo, hook_id, community_id)
        return True

    def disable_repository(self, provider_id, hook_id=None):
        """Deletes the hook for a repository and marks it as disabled."""
        # We look up the repo from `user_available_repositories` because at this point
        # we have already marked it as disabled (i.e. removed the hook ID from the DB).
        db_repo = self.user_available_repositories.filter(
            Repository.provider_id == provider_id
        ).first()
        if db_repo is None:
            raise RepositoryNotFoundError(provider_id)

        if not self.provider.delete_webhook(provider_id, hook_id):
            return False

        self.mark_repo_disabled(db_repo)
        return True
