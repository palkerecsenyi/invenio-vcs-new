# -*- coding: utf-8 -*-
# This file is part of Invenio.
# Copyright (C) 2025 CERN.
#
# Invenio is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""Models for the VCS integration."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum

from invenio_accounts.models import User
from invenio_db import db
from invenio_db.shared import Timestamp
from invenio_i18n import lazy_gettext as _
from invenio_webhooks.models import Event
from sqlalchemy import UniqueConstraint, delete, insert, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.mutable import MutableDict
from sqlalchemy_utils.types import ChoiceType, JSONType, UUIDType


class ReleaseStatus(Enum):
    """Constants for possible status of a Release."""

    __order__ = "RECEIVED PROCESSING PUBLISHED FAILED DELETED PUBLISH_PENDING"

    RECEIVED = "R"
    """Release has been received and is pending processing."""

    PROCESSING = "P"
    """Release is still being processed."""

    PUBLISHED = "D"
    """Release was successfully processed and published."""

    FAILED = "F"
    """Release processing has failed."""

    DELETED = "E"
    """Release has been deleted."""

    PUBLISH_PENDING = "S"
    """Release was processed and is pending an external action.

    In InvenioRDM, this usually means the draft was created and is being reviewed by a community.
    Release.record_is_draft will be true until this is complete.

    If this status is held by the first release for a repository, new releases may not succeed until
    the first release transitions to PUBLISHED.
    """

    def __init__(self, value):
        """Hack."""

    def __eq__(self, other):
        """Equality test."""
        return self.value == other

    def __str__(self):
        """Return its value."""
        return self.value


repository_user_association = db.Table(
    "vcs_repository_users",
    db.Model.metadata,
    db.Column(
        "repository_id",
        UUIDType,
        db.ForeignKey("vcs_repositories.id"),
        primary_key=True,
    ),
    db.Column(
        "user_id", db.Integer, db.ForeignKey("accounts_user.id"), primary_key=True
    ),
    db.Column("created", db.DateTime, nullable=False),
    db.Column("updated", db.DateTime, nullable=False),
)


class Repository(db.Model, Timestamp):
    """Information about a vcs repository."""

    __tablename__ = "vcs_repositories"

    __table_args__ = (
        UniqueConstraint(
            "provider",
            "provider_id",
            name="uq_vcs_repositories_provider_provider_id",
        ),
    )

    id = db.Column(
        UUIDType,
        primary_key=True,
        default=uuid.uuid4,
    )
    """Our internal identifier for the repository."""

    provider_id = db.Column(
        db.String(255),
        nullable=False,
    )
    """Unique identifier for the repository as given by the VCS provider.

    GH and GL give these as integers, but other VCS platforms we support in the future might not,
    so to make it as flexible as possible we store it as a string.
    The provider implementation is responsible for converting its own ID format to/from strings.

    The provider/provider_id combination is unique. A given VCS provider must not return the same
    ID for two different repositories. This does _not_ apply to the full_name: while GH/GL treat names
    as unique identifiers, this is not the case for all providers.
    """

    provider = db.Column(db.String(255), nullable=False)
    """Which VCS provider the repository is hosted by (and therefore the context in which to consider the provider_id)"""

    description = db.Column(db.String(10000), nullable=True)
    license_spdx = db.Column(db.String(255), nullable=True)
    default_branch = db.Column(db.String(255), nullable=False)

    full_name = db.Column("name", db.String(255), nullable=False)
    """Fully qualified name of the repository including user/organization."""

    hook = db.Column(db.String(255), nullable=True)
    """Webhook identifier as given by the VCS provider.

    Null if the repository is not enabled. Can also be left null if the VCS provider doesn't issue
    IDs for webhooks (e.g. if it only supports one webhook per repo).
    """

    enabled_by_user_id = db.Column(db.Integer, db.ForeignKey(User.id), nullable=True)
    """The ID of the user who last enabled the repository."""

    record_community_id = db.Column(UUIDType, nullable=True)
    """If using InvenioRDM, the default community that the first release should be submitted to.

    On instances with RDM_COMMUNITY_REQUIRED_TO_PUBLISH set to True, the release publish task will fail
    without this value set. The community submission process is handled by invenio-rdm-records.

    This is a weak reference (same as `record_id` in the Release model), so referential integrity should
    not be presumed.

    Implementations of Invenio other than InvenioRDM can also assign their own use case to this value.
    """

    #
    # Relationships
    #
    users = db.relationship(User, secondary=repository_user_association)
    enabled_by_user = db.relationship(User, foreign_keys=[enabled_by_user_id])

    @classmethod
    def create(
        cls,
        provider,
        provider_id,
        default_branch,
        full_name=None,
        description=None,
        license_spdx=None,
        **kwargs,
    ):
        """Create the repository."""
        obj = cls(
            provider=provider,
            provider_id=provider_id,
            full_name=full_name,
            default_branch=default_branch,
            description=description,
            license_spdx=license_spdx,
            **kwargs,
        )
        db.session.add(obj)
        return obj

    def add_user(self, user_id: int):
        """Add permission for a user to access the repository."""
        now = datetime.now(tz=timezone.utc)
        stmt = insert(repository_user_association).values(
            repository_id=self.id, user_id=user_id, created=now, updated=now
        )
        db.session.execute(stmt)

    def remove_user(self, user_id: int):
        """Remove permission for a user to access the repository."""
        stmt = delete(repository_user_association).filter_by(
            repository_id=self.id, user_id=user_id
        )
        db.session.execute(stmt)

    def list_users(self):
        """Return a list of users with access to the repository."""
        return db.session.execute(
            select(repository_user_association).filter_by(repository_id=self.id)
        )

    @classmethod
    def get(cls, provider: str, provider_id: str) -> Repository | None:
        """Return a repository given its provider ID.

        :param str provider: Registered ID of the VCS provider.
        :param str provider_id: VCS provider repository identifier.
        :returns: The repository object or None if one with the given ID and provider doesn't exist.
        """
        return cls.query.filter(
            Repository.provider_id == provider_id, Repository.provider == provider
        ).one_or_none()

    @property
    def enabled(self):
        """Return if the repository has webhooks enabled."""
        return bool(self.hook)

    def latest_release(
        self,
    ):
        """Chronologically latest successful release of the repository."""
        # Bail out fast if object (Repository) not in DB session.
        if self not in db.session:
            return None

        return (
            self.releases.where(Release.status == ReleaseStatus.PUBLISHED)
            .where(Release.record_is_draft == False)
            .order_by(db.desc(Release.created))
            .first()
        )

    def __repr__(self):
        """Get repository representation."""
        return "<Repository {self.full_name}:{self.provider_id}>".format(self=self)


class Release(db.Model, Timestamp):
    """Information about a VCS release."""

    __tablename__ = "vcs_releases"

    __table_args__ = (
        UniqueConstraint(
            "provider_id",
            "provider",
            name="uq_vcs_releases_provider_id_provider",
        ),
    )

    id = db.Column(
        UUIDType,
        primary_key=True,
        default=uuid.uuid4,
    )
    """Our internal identifier for the release."""

    provider_id = db.Column(db.String(255), nullable=True)
    """Unique release identifier as given by the VCS provider."""

    provider = db.Column(db.String(255), nullable=False)
    """Which VCS provider the release is hosted by (and therefore the context in which to consider the provider_id)"""

    tag = db.Column(db.String(255))
    """Release tag."""

    errors = db.Column(
        MutableDict.as_mutable(
            db.JSON()
            .with_variant(postgresql.JSONB(), "postgresql")
            .with_variant(JSONType(), "sqlite")
            .with_variant(JSONType(), "mysql")
        ),
        nullable=True,
    )
    """Release processing errors."""

    repository_id = db.Column(UUIDType, db.ForeignKey(Repository.id))
    """Repository identifier."""

    event_id = db.Column(UUIDType, db.ForeignKey(Event.id), nullable=True)
    """Incoming webhook event identifier."""

    record_id = db.Column(
        UUIDType,
        index=True,
        nullable=True,
    )
    """Weak reference to a record identifier."""

    record_is_draft = db.Column(db.Boolean(), nullable=True)
    """Whether the record referenced by `record_id` is a draft. In InvenioRDM, a record might be saved as a draft
    if publishing fails."""

    status = db.Column(
        ChoiceType(ReleaseStatus, impl=db.CHAR(1)),
        nullable=False,
    )
    """Status of the release, e.g. 'processing', 'published', 'failed', etc."""

    repository = db.relationship(
        Repository, backref=db.backref("releases", lazy="dynamic")
    )

    event = db.relationship(Event)

    def __repr__(self):
        """Get release representation."""
        return f"<Release {self.tag}:{self.provider_id} ({self.status.title})>"

    @classmethod
    def get_for_record(cls, record_id, only_draft=False) -> Release | None:
        """Get the corresponding release for a record with a specific UUID.

        :param only_draft: Only returns the release if it corresponded to a draft record on creation.
        """

        query = cls.query.filter(Release.record_id == record_id)
        if only_draft:
            query = query.filter(Release.record_is_draft == True)
        return query.one_or_none()
