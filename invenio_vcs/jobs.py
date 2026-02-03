# -*- coding: utf-8 -*-
# This file is part of Invenio.
# Copyright (C) 2026 CERN.
#
# Invenio is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""Scheduled jobs for VCS integration."""

from invenio_i18n import gettext as _
from invenio_jobs.jobs import JobType
from marshmallow import Schema, ValidationError, fields, validates

from invenio_vcs.config import get_provider_list
from invenio_vcs.tasks import refresh_accounts


class RefreshAccountsSchema(Schema):
    """
    Schema for args of RefreshAccounts job.

    This intentionally does not inherit from PredefinedArgsSchema since we do not want
    to use the `since` arg.
    """

    provider = fields.String(
        required=False,
        metadata={
            "description": _(
                "The name of the locally configured provider to sync accounts for. "
                "If not specified, will use the first entry in the list of configured "
                "providers."
            )
        },
    )

    limit = fields.Int(
        required=False,
        metadata={
            "description": _(
                "The maximum number of accounts to sync in one run. Running the job "
                "multiple times will make it continue handling further accounts. "
                "The default value is 1000. Specifying 0 removes the limit."
            )
        },
    )

    min_age = fields.Int(
        required=False,
        metadata={
            "description": _(
                "The minimum time (in days) that needs to have elapsed since the last "
                "sync for an account to be included in the list of accounts to sync. "
                "The default value is 30 (days). Using 0 will cause all accounts to be "
                "considered."
            )
        },
    )

    job_arg_schema = fields.String(
        metadata={"type": "hidden"},
        dump_default="RefreshAccountsSchema",
        load_default="RefreshAccountsSchema",
    )
    """Hidden field needed for identifying the schema (see `PredefinedArgsSchema`)."""

    @validates("provider")
    def validate_provider(self, value: str | None) -> None:
        """Ensure the provider is registered on the instance."""
        provider_list = get_provider_list()
        if value is None:
            if len(provider_list) == 0:
                raise ValidationError(_("No VCS providers configured on the instance."))

            return

        for provider in provider_list:
            if provider.id == value:
                return

        raise ValidationError(
            _("Provider with ID %(value)s not registered on instance.", value=value)
        )


class RefreshAccountsJob(JobType):
    task = refresh_accounts
    title = _("Refresh VCS accounts")
    description = _("Refresh stale VCS accounts and re-sync their repository metadata")
    id = "refresh_vcs_accounts"
    arguments_schema = RefreshAccountsSchema

    @classmethod
    def build_task_arguments(
        cls, job_obj, provider=None, limit=None, min_age=None, **kwargs
    ):
        """Build task arguments."""
        if provider is None:
            provider_list = get_provider_list()
            assert len(provider_list) != 0
            provider = provider_list[0].id

        return {"provider": provider, "limit": limit, "min_age": min_age}
