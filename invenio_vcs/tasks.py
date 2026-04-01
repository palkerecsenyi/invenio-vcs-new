# -*- coding: utf-8 -*-
# This file is part of Invenio.
# Copyright (C) 2025 CERN.
# Copyright (C) 2024 KTH Royal Institute of Technology.
#
# Invenio is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""Task for managing vcs integration."""

import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from celery import shared_task
from flask import current_app, g
from invenio_db import db
from invenio_i18n import gettext as _
from invenio_oauthclient.models import RemoteAccount
from invenio_oauthclient.proxies import current_oauthclient

from invenio_vcs.config import get_provider_by_id
from invenio_vcs.errors import CustomVCSReleaseNoRetryError, RepositoryAccessError
from invenio_vcs.models import Release, ReleaseStatus
from invenio_vcs.proxies import current_vcs

if TYPE_CHECKING:
    from invenio_vcs.service import VCSRelease


def _get_err_obj(msg):
    """Generate the error entry with a Sentry ID."""
    err = {"errors": msg}
    if hasattr(g, "sentry_event_id"):
        err["error_id"] = str(g.sentry_event_id)
    return err


def release_default_exception_handler(release: "VCSRelease", ex):
    """Default handler."""
    release.db_release.errors = _get_err_obj(str(ex))
    db.session.commit()


DEFAULT_ERROR_HANDLERS = [
    (CustomVCSReleaseNoRetryError, release_default_exception_handler),
    (Exception, release_default_exception_handler),
]


@shared_task(max_retries=6, default_retry_delay=10 * 60, rate_limit="100/m")
def disconnect_provider(provider_id, user_id, access_token, repo_hooks):
    """Uninstall webhooks."""
    # Note at this point the remote account and all associated data have
    # already been deleted. The celery task is passed the access_token to make
    # some last cleanup and afterwards delete itself remotely.

    # Local import to avoid circular imports
    from .service import VCSService

    try:
        # Create a nested transaction to make sure that hook deletion + token revoke is atomic
        with db.session.begin_nested():
            svc = VCSService.for_provider_and_token(provider_id, user_id, access_token)

            for repo_id, repo_hook in repo_hooks:
                if svc.disable_repository(repo_id, repo_hook):
                    current_app.logger.info(
                        _("Deleted hook from vcs repository."),
                        extra={"hook": repo_hook, "repo": repo_id},
                    )

            # If we finished our clean-up successfully, we can revoke the token
            svc.provider.revoke_token(access_token)
    except Exception as exc:
        # Retry in case vcs may be down...
        disconnect_provider.retry(exc=exc)


@shared_task(max_retries=6, default_retry_delay=10 * 60, rate_limit="100/m")
def sync_hooks(provider, user_id, repositories):
    """Sync repository hooks for a user."""
    # Local import to avoid circular imports
    from .service import VCSService

    try:
        # Sync hooks
        svc = VCSService.for_provider_and_user(provider, user_id)
        for repo_id in repositories:
            try:
                with db.session.begin_nested():
                    svc.sync_repo_hook(repo_id)
                # We commit per repository, because the task can run for potentially
                # thousands of repos. Each of them is completely independent, so committing
                # early does not cause a problem in case of an Exception that occurs before
                # all repos have been processed.
                db.session.commit()
            except RepositoryAccessError as e:
                current_app.logger.warning(str(e), exc_info=True)
    except Exception as exc:
        current_app.logger.warning(str(exc), exc_info=True)
        sync_hooks.retry(exc=exc)


@shared_task(max_retries=6, default_retry_delay=10 * 60, rate_limit="100/m")
def sync_repo_users(provider, user_id, repo_provider_ids):
    """Sync the Invenio users that have access to a repo.

    A user ID is still required so we know which user's OAuth credentials to use.
    """
    from .service import VCSService

    try:
        svc = VCSService.for_provider_and_user(provider, user_id)

        for repo_id in repo_provider_ids:
            try:
                with db.session.begin_nested():
                    svc.sync_repo_users(repo_id)
                db.session.commit()
            except RepositoryAccessError as e:
                current_app.logger.warning(str(e), exc_info=True)
    except Exception as exc:
        current_app.logger.warning(str(exc), exc_info=True)
        raise sync_repo_users.retry(exc=exc)


@shared_task(ignore_result=True, max_retries=5, default_retry_delay=10 * 60)
def process_release(provider, release_id):
    """Process a received Release."""
    release_model = Release.query.filter(
        Release.provider_id == release_id,
        Release.status.in_([ReleaseStatus.RECEIVED, ReleaseStatus.FAILED]),
    ).one()

    provider = get_provider_by_id(provider).for_user(
        release_model.repository.enabled_by_user_id
    )
    release = current_vcs.release_api_class(release_model, provider)

    # Mark the release as processing so users can see the status in case
    # `process_release` is a long-running method. We need to commit so the
    # status is visible to the user before the full task finishes.
    release_model.status = ReleaseStatus.PROCESSING
    db.session.commit()

    matched_error_cls = None
    matched_ex = None

    try:
        release.process_release()
        db.session.commit()
    except Exception as ex:
        error_handlers = current_vcs.release_error_handlers
        matched_ex = None
        for error_cls, handler in error_handlers + DEFAULT_ERROR_HANDLERS:
            if isinstance(ex, error_cls):
                handler(release, ex)
                matched_error_cls = error_cls
                matched_ex = ex
                break

    if matched_error_cls is Exception:
        process_release.retry(ex=matched_ex)


@shared_task(ignore_result=True)
def refresh_accounts(provider: str, limit=1000, min_age=2592000):
    """
    Run the repository sync for accounts registered with a provider.

    All accounts that were last synced at least `min_age` seconds ago will be synced
    **in the background** up to `limit` amount. All data will be synced, including
    the webhook's activation state and the list of users who have access to each repo.

    This task **will not** wait for each sync to complete, it will just schedule the
    tasks for each account, so it should complete relatively quickly.
    """
    remote = current_oauthclient.oauth.remote_apps.get(provider)
    if remote is None:
        raise ValueError(
            f"Provider {provider} not found as a registered OAuth remote app."
        )

    updated_before = datetime.now(tz=timezone.utc) - timedelta(seconds=min_age)

    # Find remote accounts that have not been updated since `updated_before`
    remote_accounts_to_be_updated = RemoteAccount.query.filter(
        RemoteAccount.updated < updated_before,
        RemoteAccount.client_id == remote.consumer_key,
    )
    if limit != 0:
        remote_accounts_to_be_updated = remote_accounts_to_be_updated.limit(limit)

    tasks = 0
    for remote_account in remote_accounts_to_be_updated:
        sync_account.delay(provider, remote_account.user_id)
        tasks += 1

    logging.info(f"Triggered {tasks} account sync tasks.")


@shared_task(ignore_result=True)
def sync_account(provider, user_id):
    """Sync a user account."""
    # Local import to avoid circular imports
    from .service import VCSService

    svc = VCSService.for_provider_and_user(provider, user_id)
    svc.sync()
