# -*- coding: utf-8 -*-
# This file is part of Invenio.
# Copyright (C) 2025-2026 CERN.
#
# Invenio is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.
#
# Some of the code in this file was taken from https://codebase.helmholtz.cloud/rodare/invenio-gitlab
# and relicensed under MIT with permission from the authors.
"""Contrib provider implementation for GitLab."""

from __future__ import annotations

from typing import Any

import dateutil
import gitlab
import gitlab.const
import requests
from flask import current_app
from invenio_i18n import gettext as _
from invenio_oauthclient import current_oauthclient
from werkzeug.utils import cached_property

from invenio_vcs.errors import VCSTokenNotFound
from invenio_vcs.generic_models import (
    GenericContributor,
    GenericOwner,
    GenericOwnerType,
    GenericRelease,
    GenericRepository,
    GenericUser,
    GenericWebhook,
)
from invenio_vcs.providers import (
    RepositoryServiceProvider,
    RepositoryServiceProviderFactory,
)


def _gitlab_response_error_handler(f):
    """Handle common error codes returned by the API."""

    def inner_function(*args, **kwargs):
        try:
            return f(*args, **kwargs)
        except gitlab.GitlabGetError as e:
            if e.response_code == 404:
                return None
            else:
                raise e
        except gitlab.GitlabCreateError as e:
            if e.response_code == 404:
                return None
            else:
                raise e

    return inner_function


class GitLabProviderFactory(RepositoryServiceProviderFactory):
    """Contrib implementation factory for GitLab."""

    def __init__(
        self,
        base_url: str,
        webhook_receiver_url: str,
        id="gitlab",
        name="GitLab",
        description=_("Automatically archive your GitLab projects"),
        credentials_key="GITLAB_APP_CREDENTIALS",
        config={},
    ):
        """Initialise with GitLab-specific defaults."""
        super().__init__(
            GitLabProvider,
            base_url=base_url,
            webhook_receiver_url=webhook_receiver_url,
            id=id,
            name=name,
            description=description,
            credentials_key=credentials_key,
            icon="gitlab",
            repository_name=_("project"),
            repository_name_plural=_("projects"),
            # The base URL inside this doesn't get updated if `base_url` is overridden in update_config_override.
            # If you change the base URL after calling the constructor, make sure to change this too.
            release_docs_link=f"{base_url}/help/user/project/releases/_index.md",
            repo_list_message=_(
                "If your project does not show up in the list, please ensure you have at least the 'Maintainer' role on the project or its group."
            ),
            repo_list_info_link=f"{base_url}/help/user/permissions.md#project-permissions",
        )

        self._gitlab_specific_config = dict()
        self._gitlab_specific_config.update(shared_validation_token="")
        self._gitlab_specific_config.update(config)

    def update_config_with_override(self, config_override: dict):
        """Allow overriding GitLab-specific config options."""
        super().update_config_with_override(config_override)
        self._gitlab_specific_config.update(config_override.get("config", {}))

    def _account_info_handler(self, remote, resp: dict):
        """Helper for the OAuth client."""
        gl = gitlab.Gitlab(
            self.base_url,
            oauth_token=resp["access_token"],
        )
        gl.auth()
        user_attrs = gl.user.attributes
        handlers = current_oauthclient.signup_handlers[remote.name]
        # Pass through `info_serializer` which converts the user to an Invenio user (and performs additional validation).
        return handlers["info_serializer"](resp, user_attrs)

    def _account_info_serializer(self, remote, resp, user_info, **kwargs):
        """Helper for the OAuth client."""
        return dict(
            user=dict(
                email=user_info["email"],
                profile=dict(
                    username=user_info["username"],
                    full_name=user_info["name"],
                ),
            ),
            external_id=str(user_info["id"]),
            external_method="gitlab",
        )

    @property
    def oauth_remote_config(self):
        """Custom OAuth client config for GitLab."""
        return dict(
            title=self.name,
            description=self.description,
            icon="fa fa-{}".format(self.icon),
            authorized_handler="invenio_oauthclient.handlers:authorized_signup_handler",
            disconnect_handler=self.oauth_handlers.disconnect_handler,
            signup_handler=dict(
                info=self._account_info_handler,
                info_serializer=self._account_info_serializer,
                setup=self.oauth_handlers.account_setup_handler,
                view="invenio_oauthclient.handlers:signup_handler",
            ),
            params=dict(
                base_url=f"{self.base_url}/api/v4/",
                request_token_url=None,
                access_token_url=f"{self.base_url}/oauth/token",
                access_token_method="POST",
                authorize_url=f"{self.base_url}/oauth/authorize",
                app_key=self.credentials_key,
            ),
        )

    @property
    def provider_specific_config(self):
        """Returns the GitLab-specific config dict."""
        return self._gitlab_specific_config

    def url_for_repository(self, repository_name: str) -> str:
        """URL for viewing a repository."""
        return f"{self.base_url}/{repository_name}"

    def url_for_release(
        self, repository_name: str, release_id: str, release_tag: str
    ) -> str:
        """URL for viewing a release."""
        return f"{self.base_url}/{repository_name}/-/releases/{release_tag}"

    def url_for_tag(self, repository_name, tag_name) -> str:
        """The URL for viewing a tag."""
        return f"{self.base_url}/{repository_name}/-/tags/{tag_name}"

    def url_for_new_file(self, repository_name, branch_name, file_name) -> str:
        """The URL for creating a new file in the web editor."""
        return f"{self.base_url}/{repository_name}/-/new/{branch_name}/?file_name={file_name}"

    def url_for_new_release(self, repository_name) -> str:
        """The URL for creating a new release."""
        return f"{self.base_url}/{repository_name}/-/releases/new"

    def url_for_new_repo(self) -> str:
        """The URL for creating a new repository."""
        return f"{self.base_url}/projects/new"

    def webhook_is_create_release_event(self, event_payload: dict[str, Any]):
        """Identify if the webhook payload is one we want to use."""
        # https://archives.docs.gitlab.com/17.11/user/project/integrations/webhook_events/#release-events

        # GitLab does not have unpublished/draft releases the way GitHub does. However, it does have
        # "upcoming releases" (https://archives.docs.gitlab.com/17.11/api/releases/#upcoming-releases)
        # meaning ones with a release date in the future.
        # TODO: do we want to return False for upcoming releases?

        object_kind = event_payload.get("object_kind")
        action = event_payload.get("action")

        # existing `invenio-gitlab` instead uses the `tag_push` event which is more general than the `release`
        # event (https://codebase.helmholtz.cloud/rodare/invenio-gitlab/-/blob/d66181697b8a34383b333306b559d13cd6fa829a/invenio_gitlab/receivers.py#L41).
        # TODO: I recommend using the `release` event as this is a more 'formal' manual action and better corresponds to the release event in GitHub. Is this okay?
        return object_kind == "release" and action == "create"

    def webhook_event_to_generic_release(
        self, event_payload: dict[str, Any]
    ) -> GenericRelease:
        """Convert a webhook event."""
        # https://archives.docs.gitlab.com/18.0/user/project/integrations/webhook_events/#release-events
        # https://archives.docs.gitlab.com/17.11/user/project/integrations/webhook_events/#release-events
        # https://archives.docs.gitlab.com/16.11/ee/user/project/integrations/webhook_events.html#release-events

        zipball_url: str | None = None
        tarball_url: str | None = None

        for source in event_payload["assets"]["sources"]:
            format = source["format"]
            url = source["url"]
            if format == "zip":
                zipball_url = url
            elif format == "tar":
                tarball_url = url

        release = GenericRelease(
            id=str(event_payload["id"]),
            repository_id=str(event_payload["project"]["id"]),
            tag_name=event_payload["tag"],
            name=event_payload["name"],
            body=event_payload["description"],
            zipball_url=zipball_url,
            tarball_url=tarball_url,
            created_at=dateutil.parser.parse(event_payload["created_at"]),
            published_at=dateutil.parser.parse(event_payload["released_at"]),
        )

        return release

    @staticmethod
    def _extract_license(proj_attrs: dict[str, Any]):
        """Extract the SPDX ID from the license of a dict-ified project."""
        license_obj = proj_attrs.get("license")
        if license_obj is not None:
            return license_obj["key"].upper()
        return None


class GitLabProvider(RepositoryServiceProvider):
    """Contrib user-specific implementation for GitLab."""

    @cached_property
    def _gitlab(self):
        """Construct the GitLab API client and make a test auth request (which populates essential data)."""
        if self.oauth_remote_token is None:
            raise VCSTokenNotFound
        api = gitlab.Gitlab(
            self.factory.base_url, oauth_token=self.oauth_remote_token.access_token
        )
        api.auth()
        return api

    @_gitlab_response_error_handler
    def list_repositories(self) -> dict[str, GenericRepository] | None:
        """List all projects."""
        repos: dict[str, GenericRepository] = {}
        for project in self._gitlab.projects.list(
            iterator=True,
            simple=False,
            min_access_level=gitlab.const.MAINTAINER_ACCESS,
        ):
            repos[str(project.id)] = GenericRepository(
                id=str(project.id),
                full_name=project.path_with_namespace,
                default_branch=project.default_branch,
                description=project.description,
                # The license is not returned in the project list, and it is not needed here.
                license_spdx=None,
            )
        return repos

    @_gitlab_response_error_handler
    def get_repository(self, repository_id: str) -> GenericRepository | None:
        """Get a single project."""
        assert repository_id.isdigit()
        proj = self._gitlab.projects.get(int(repository_id), license=True)
        return GenericRepository(
            id=str(proj.id),
            full_name=proj.path_with_namespace,
            default_branch=proj.default_branch,
            description=proj.description,
            license_spdx=GitLabProviderFactory._extract_license(proj.asdict()),
        )

    @_gitlab_response_error_handler
    def list_repository_contributors(
        self, repository_id: str, max: int
    ) -> list[GenericContributor] | None:
        """Get and enrich the contributor list as much as possible with the limited data returned by the API."""
        assert repository_id.isdigit()
        proj = self._gitlab.projects.get(int(repository_id), lazy=True)

        contribs: list[GenericContributor] = []
        for index, contrib in enumerate(
            proj.repository_contributors(iterator=True, order_by="commits", sort="desc")
        ):
            email = contrib["email"]
            contrib_count = contrib["commits"]

            # repository_contributors returns a very small amount of data (not even the username)
            # See here https://archives.docs.gitlab.com/17.11/api/repositories/#contributors
            # So we try to enrich the data by searching for the user with the matching email.
            # We will fail to find it if a) the user doesn't exist (e.g. repos imported/forked from somewhere else)
            # or b) if the user has not made their email address public.
            # By default, email addresses on GitLab are private, so this is unlikely to succeed.
            matching_users = self._gitlab.users.list(search=email)
            if len(matching_users) == 0:
                contribs.append(
                    GenericContributor(
                        id=email,
                        username=email,
                        display_name=contrib["name"],
                        contributions_count=contrib_count,
                    )
                )
            else:
                matching_user = matching_users[0]
                contribs.append(
                    GenericContributor(
                        id=str(matching_user.id),
                        username=matching_user.username,
                        display_name=matching_user.name,
                        contributions_count=contrib_count,
                    )
                )

            if index + 1 == max:
                break

        return contribs

    @_gitlab_response_error_handler
    def get_repository_owner(self, repository_id: str):
        """Get the owner of the project."""
        assert repository_id.isdigit()
        proj = self._gitlab.projects.get(int(repository_id))
        return GenericOwner(
            id=str(proj.namespace["id"]),
            path_name=proj.namespace["path"],
            display_name=proj.namespace["name"],
            type=(
                GenericOwnerType.USER
                if proj.namespace["kind"] == "user"
                else GenericOwnerType.ORGANIZATION
            ),
        )

    @_gitlab_response_error_handler
    def list_repository_user_ids(self, repository_id: str) -> list[str] | None:
        """See https://docs.gitlab.com/api/members/#list-all-members-of-a-group-or-project-including-inherited-and-invited-members."""
        user_ids: list[str] = []
        for member in self._gitlab.projects.get(
            int(repository_id), lazy=True
        ).members_all.list(iterator=True):
            if member.access_level >= gitlab.const.MAINTAINER_ACCESS:
                user_ids.append(str(member.id))
        return user_ids

    @_gitlab_response_error_handler
    def list_repository_webhooks(
        self, repository_id: str
    ) -> list[GenericWebhook] | None:
        """Convert the repository's webhooks to a generic list."""
        assert repository_id.isdigit()
        proj = self._gitlab.projects.get(int(repository_id), lazy=True)
        hooks: list[GenericWebhook] = []
        for hook in proj.hooks.list(iterator=True):
            hooks.append(
                GenericWebhook(
                    id=str(hook.id),
                    repository_id=str(hook.project_id),
                    url=hook.url,
                )
            )
        return hooks

    @property
    def _hook_data(self):
        """Reusable GitLab-specific config passed to the hooks API."""
        return {
            "url": self.webhook_url,
            "token": self.factory.provider_specific_config.get(
                "shared_validation_token"
            ),
            "releases_events": True,
            # For some reason, we need to specify this as False explicitly. A default value of True seems to be
            # assumed but is not documented anywhere. For all other event types, default is False.
            "push_events": False,
            "description": _(
                "Managed by %(app_name)s. Please do not edit.",
                app_name=current_app.config.get("THEME_SITENAME", "Invenio"),
            ),
        }

    @_gitlab_response_error_handler
    def create_webhook(self, repository_id: str) -> str | None:
        """Create a webhook with a metadata description to avoid confusion."""
        assert repository_id.isdigit()
        proj = self._gitlab.projects.get(int(repository_id), lazy=True)
        resp = proj.hooks.create(self._hook_data)
        return str(resp.id)

    @_gitlab_response_error_handler
    def delete_webhook(self, repository_id: str, hook_id=None) -> bool:
        """Delete the hook from the project if it exists."""
        assert repository_id.isdigit()
        if hook_id is not None:
            assert hook_id.isdigit()

        proj = self._gitlab.projects.get(int(repository_id), lazy=True)
        if hook_id is None:
            existing_hook = self.get_configured_webhook(repository_id)
            if existing_hook is None:
                return True

            proj.hooks.delete(int(existing_hook.id))
        else:
            proj.hooks.delete(int(hook_id))

        return True

    @_gitlab_response_error_handler
    def update_webhook(self, repository_id: str, hook_id: str) -> bool:
        """Update the webhook with the current `self._hook_data`."""
        assert repository_id.isdigit()
        assert hook_id.isdigit()

        proj = self._gitlab.projects.get(int(repository_id), lazy=True)
        hook = proj.hooks.get(int(hook_id))
        for key, value in self._hook_data.items():
            setattr(hook, key, value)
        hook.save()
        return True

    @_gitlab_response_error_handler
    def get_own_user(self) -> GenericUser | None:
        """Return the currently signed in user."""
        user = self._gitlab.user
        if user is None:
            return None
        return GenericUser(
            id=str(user.id),
            username=user.username,
            display_name=user.name,
        )

    def resolve_release_zipball_url(self, release_zipball_url: str) -> str | None:
        """No further resolution needs to be done for GitLab, so this is a no-op."""
        return release_zipball_url

    @_gitlab_response_error_handler
    def fetch_release_zipball(self, release_zipball_url: str, timeout: int):
        """Make a raw request with the API token to download the file."""
        resp = self._gitlab.http_get(
            release_zipball_url, raw=True, streamed=True, timeout=timeout
        )
        assert isinstance(resp, requests.Response)
        with resp:
            yield resp.raw

    @_gitlab_response_error_handler
    def retrieve_remote_file(self, repository_id: str, ref_name: str, file_name: str):
        """Download and decode the given file using the API."""
        assert repository_id.isdigit()
        proj = self._gitlab.projects.get(int(repository_id), lazy=True)
        try:
            file = proj.files.get(file_path=file_name, ref=ref_name)
            return file.decode()
        except gitlab.GitlabGetError:
            return None

    def revoke_token(self, access_token: str):
        """TODO: GitLab implements RFC7009 for OAuth Token Revocation. We might need to do this via OAuth instead of the GitLab API."""
        pass
