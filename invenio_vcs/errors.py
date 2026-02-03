# -*- coding: utf-8 -*-
# This file is part of Invenio.
# Copyright (C) 2025 CERN.
# Copyright (C) 2024 KTH Royal Institute of Technology.
#
# Invenio is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""Invenio-vcs errors."""

from invenio_i18n import gettext as _


class VCSError(Exception):
    """General vcs error."""


class RepositoryAccessError(VCSError):
    """Repository access permissions error."""

    message = _("The user cannot access this repository")

    def __init__(self, user=None, repo=None, repo_id=None, message=None):
        """Constructor."""
        super().__init__(message or self.message)
        self.message = message
        self.user = user
        self.repo = repo
        self.repo_id = repo_id


class RepositoryDisabledError(VCSError):
    """Repository access permissions error."""

    message = _("This repository is not enabled for webhooks.")

    def __init__(self, repo=None, message=None):
        """Constructor."""
        super().__init__(message or self.message)
        self.repo = repo


class RepositoryNotFoundError(VCSError):
    """Repository not found error."""

    message = _("The repository does not exist.")

    def __init__(self, repo=None, message=None):
        """Constructor."""
        super().__init__(message or self.message)
        self.repo = repo


class InvalidSenderError(VCSError):
    """Invalid release sender error."""

    message = _("Invalid sender for event")

    def __init__(self, event=None, user=None, message=None):
        """Constructor."""
        super().__init__(message or self.message)
        self.event = event
        self.user = user


class ReleaseAlreadyReceivedError(VCSError):
    """Invalid release sender error."""

    message = _("The release has already been received.")

    def __init__(self, release=None, message=None):
        """Constructor."""
        super().__init__(message or self.message)
        self.release = release


class CustomVCSReleaseNoRetryError(VCSError):
    """An error prevented the release from being published, but the publish should not be retried.

    This error simply wraps a more specific error `message` and serves as an indicator. During the task,
    if this error is received the publish will not be retried.
    If the error is due to something that the user needs to fix, the method raising the error is responsible
    for conveying this information to the user, e.g. through a notification.
    """

    def __init__(self, message=None):
        """Constructor."""
        super().__init__(message)


class VCSTokenNotFound(VCSError):
    """OAuth session token was not found."""

    message = _("The OAuth session token was not found.")

    def __init__(self, user=None, message=None):
        """Constructor."""
        super().__init__(message or self.message)
        self.user = user


class RemoteAccountNotFound(VCSError):
    """Remote account for the user is not setup."""

    message = _("RemoteAccount not found for user")

    def __init__(self, user=None, message=None):
        """Constructor."""
        super().__init__(message or self.message)
        self.user = user


class RemoteAccountDataNotSet(VCSError):
    """Remote account extra data for the user is not set."""

    message = _("RemoteAccount extra data not set for user.")

    def __init__(self, user=None, message=None):
        """Constructor."""
        super().__init__(message or self.message)
        self.user = user


class ReleaseNotFound(VCSError):
    """Release does not exist."""

    message = _("Release does not exist.")

    def __init__(self, message=None):
        """Constructor."""
        super().__init__(message or self.message)


class UnexpectedProviderResponse(VCSError):
    """Request to VCS API returned an unexpected error."""

    message = _("Provider API returned an unexpected error.")

    def __init__(self, message=None):
        """Constructor."""
        super().__init__(message or self.message)


class ReleaseZipballFetchError(VCSError):
    """Error fetching release zipball file."""

    message = _("Error fetching release zipball file.")

    def __init__(self, message=None):
        """Constructor."""
        super().__init__(message or self.message)


class UserInfoNoneError(VCSError):
    """VCS provider did not return profile info."""

    message = _("Provider did not return user profile information.")

    def __init__(self, message=None) -> None:
        """Constructor."""
        super().__init__(message or self.message)


class MultipleWebhooksError(VCSError):
    """VCS provider returned multiple webhooks that matched this Invenio instance.

    We should only ever create one such matching webhook, so having multiple is an invalid
    state. To prevent unexpected bugs, we raise this exception instead of ignoring it.
    """

    message = _("Multiple existing webhooks found.")

    def __init__(self, repo_provider_id: str, message=None) -> None:
        super().__init__(message or self.message)
        self.repo_provider_id = repo_provider_id
