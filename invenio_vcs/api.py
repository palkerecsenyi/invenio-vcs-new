# -*- coding: utf-8 -*-
# This file is part of Invenio.
# Copyright (C) 2026 CERN.
#
# Invenio is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.
"""High-level release wrapper to allow instance-specific implementation."""

from contextlib import contextmanager
from typing import TYPE_CHECKING

from flask import current_app
from invenio_access.permissions import authenticated_user
from invenio_access.utils import get_identity
from werkzeug.utils import cached_property

from invenio_vcs.generic_models import GenericRepository
from invenio_vcs.models import Release, Repository

if TYPE_CHECKING:
    from invenio_vcs.providers import RepositoryServiceProvider


class VCSRelease:
    """
    Represents a release and common high-level operations that can be performed on it.

    This class is often overriden upstream (e.g. in `invenio-rdm-records`) to specify
    what a 'publish' event should do on a given Invenio implementation.
    This module does not attempt to publish a record or anything similar, as `invenio-vcs`
    is designed to work on any Invenio instance (not just RDM).
    """

    def __init__(self, release: Release, provider: "RepositoryServiceProvider"):
        """Constructor."""
        self.db_release = release
        self.provider = provider
        self._resolved_zipball_url = None

    @cached_property
    def record(self):
        """Release record."""
        return self.resolve_record()

    @cached_property
    def event(self):
        """Get release event."""
        return self.db_release.event

    @cached_property
    def payload(self):
        """Return event payload."""
        return self.event.payload

    @cached_property
    def generic_release(self):
        """Converts the VCS-specific payload into a GenericRelease."""
        return self.provider.factory.webhook_event_to_generic_release(self.payload)

    @cached_property
    def generic_repo(self) -> "GenericRepository":
        """Return repo metadata."""
        repo = self.provider.get_repository(self.generic_release.repository_id)
        # It would not make sense for the webhook to return a repository ID that corresponds to a
        # non-existent repository, so we can safely assert this.
        assert repo is not None
        return repo

    @cached_property
    def db_repo(self) -> Repository:
        """Return repository model from database."""
        if self.db_release.repository_id:
            repository = self.db_release.repository
        else:
            repository = Repository.query.filter_by(
                user_id=self.event.user_id, provider_id=self.provider.factory.id
            ).one()
        return repository

    @cached_property
    def release_file_name(self):
        """Returns release zipball file name."""
        tag_name = self.generic_release.tag_name
        repo_name = self.generic_repo.full_name
        filename = f"{repo_name}-{tag_name}.zip"
        return filename

    @cached_property
    def release_zipball_url(self):
        """Returns the release zipball URL."""
        return self.generic_release.zipball_url

    @cached_property
    def user_identity(self):
        """Generates release owner's user identity."""
        identity = get_identity(self.db_repo.enabled_by_user)
        identity.provides.add(authenticated_user)
        identity.user = self.db_repo.enabled_by_user
        return identity

    @cached_property
    def contributors(self):
        """Get list of contributors to a repository.

        The list of contributors is fetched from the VCS, filtered for type "User" and sorted by contributions.

        :returns: a generator of objects that contains contributors information.
        """
        max_contributors = current_app.config.get("VCS_MAX_CONTRIBUTORS_NUMBER", 30)
        return self.provider.list_repository_contributors(
            self.db_repo.provider_id, max=max_contributors
        )

    @cached_property
    def owner(self):
        """Get owner of repository as a creator."""
        try:
            return self.provider.get_repository_owner(self.db_repo.provider_id)
        except Exception:
            return None

    # Helper functions

    def is_first_release(self):
        """Checks whether the current release is the first successful release of the repository."""
        latest_release = self.db_repo.latest_release()
        return True if not latest_release else False

    def resolve_zipball_url(self, cache=True):
        """Resolve the zipball URL.

        This method will try to resolve the zipball URL by making a HEAD request,
        handling the following edge cases:

        - In the case of a 300 Multiple Choices response, which can happen when a tag
          and branch have the same name, it will try to fetch an "alternate" link.
        - If the access token does not have the required scopes/permissions to access
          public links, it will fallback to a non-authenticated request.
        """
        if self._resolved_zipball_url and cache:
            return self._resolved_zipball_url

        url = self.release_zipball_url
        url = self.provider.resolve_release_zipball_url(url)

        if cache:
            self._resolved_zipball_url = url

        return url

    # High level API

    @contextmanager
    def fetch_zipball_file(self):
        """Fetch release zipball file using the current VCS session."""
        timeout = current_app.config.get("VCS_ZIPBALL_TIMEOUT", 300)
        zipball_url = self.resolve_zipball_url()
        return self.provider.fetch_release_zipball(zipball_url, timeout)

    def process_release(self):
        """Processes the VCS release represented by this class instance.

        The implementation of this is specified at a higher level, for example in InvenioRDM.
        The actions that occur when a release is received are not specified by InvenioVCS.

        This method is called inside the `process_release` Celery task. If an exception is raised,
        the call to this method will be retried up to 5 times. To prevent the task from being retried,
        wrap the exception in `CustomVCSReleaseNoRetryError`.
        """
        raise NotImplementedError

    @property
    def badge_title(self):
        """Stores a string to render in the record badge title (e.g. 'DOI')."""
        return None

    @property
    def badge_value(self):
        """Stores a string to render in the record badge value (e.g. '10.1234/invenio.1234')."""
        raise NotImplementedError

    @property
    def record_url(self):
        """Release self url (e.g. VCS HTML url)."""
        raise NotImplementedError
