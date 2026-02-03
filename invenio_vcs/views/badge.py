# -*- coding: utf-8 -*-
# This file is part of Invenio.
# Copyright (C) 2014-2025 CERN.
#
# Invenio is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""DOI Badge Blueprint."""

from __future__ import absolute_import

from flask import Blueprint, abort, redirect, url_for
from flask_login import current_user

from invenio_vcs.config import get_provider_by_id
from invenio_vcs.models import Repository
from invenio_vcs.proxies import current_vcs

blueprint = Blueprint(
    "invenio_vcs_badge",
    __name__,
    url_prefix="/badge/<provider>",
    static_folder="../static",
    template_folder="../templates",
)


@blueprint.route("/<repo_provider_id>.svg")
def index(provider, repo_provider_id):
    """Generate a badge for a specific vcs repository (by vcs ID)."""
    repo = Repository.query.filter(
        Repository.provider_id == repo_provider_id, Repository.provider == provider
    ).one_or_none()
    if not repo:
        abort(404)

    latest_release = repo.latest_release()
    if not latest_release:
        abort(404)

    provider = get_provider_by_id(provider).for_user(current_user.id)
    release = current_vcs.release_api_class(latest_release, provider)

    # release.badge_title points to "DOI"
    # release.badge_value points to the record "pids.doi.identifier"
    badge_url = url_for(
        "invenio_formatter_badges.badge",
        title=release.badge_title,
        value=release.badge_value,
        ext="svg",
    )
    return redirect(badge_url)
