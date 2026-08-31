import hashlib
import io
import zipfile
from dataclasses import replace

import pytest
from sqlalchemy import select
from test_integration_service import assert_rolled_back, execute, prepare

from coifesp_harness.delivery.bundle import BundleArtifact, assemble_delivery_bundle
from coifesp_harness.delivery.repository import INTEGRATION_RUNS
from coifesp_harness.errors import GovernanceConflictError
from coifesp_harness.product.repository import PROJECT_RESOURCES


@pytest.mark.parametrize("mutation", ["extra_file", "manifest", "digest", "different_bytes", "compressed"])
def test_publisher_rejects_forged_or_noncanonical_zip(tmp_path, mutation):
    value = prepare(tmp_path)
    publish = value.integration.publisher.publish

    def forged(connection, *, integration_id, bundle, mutation_fence):
        if mutation == "extra_file":
            stream = io.BytesIO(bundle.content)
            with zipfile.ZipFile(stream, "a") as archive:
                archive.writestr("unapproved-secret", b"private data")
            bundle = replace(bundle, content=stream.getvalue(), sha256=hashlib.sha256(stream.getvalue()).hexdigest())
        elif mutation == "manifest":
            bundle = replace(bundle, manifest=b"{}")
        elif mutation == "digest":
            bundle = replace(bundle, sha256="0" * 64)
        elif mutation == "different_bytes":
            bundle = assemble_delivery_bundle([BundleArtifact("resource-input", "1", "Input",
                "text/plain", hashlib.sha256(b"other").hexdigest(), b"other")])
        else:
            stream = io.BytesIO()
            with zipfile.ZipFile(io.BytesIO(bundle.content)) as source, zipfile.ZipFile(
                    stream, "w", compression=zipfile.ZIP_DEFLATED) as target:
                for name in source.namelist():
                    target.writestr(name, source.read(name))
            bundle = replace(bundle, content=stream.getvalue(), sha256=hashlib.sha256(stream.getvalue()).hexdigest())
        return publish(connection, integration_id=integration_id, bundle=bundle, mutation_fence=mutation_fence)

    value.integration.publisher.publish = forged
    with pytest.raises(GovernanceConflictError):
        execute(value)
    assert_rolled_back(value)


def test_publisher_duplicate_in_caller_transaction_reuses_same_resource(tmp_path):
    value = prepare(tmp_path)
    publish = value.integration.publisher.publish

    def twice(*args, **kwargs):
        first = publish(*args, **kwargs)
        assert publish(*args, **kwargs) == first
        return first

    value.integration.publisher.publish = twice
    result = execute(value)
    with value.engine.connect() as connection:
        rows = connection.execute(select(PROJECT_RESOURCES).where(
            PROJECT_RESOURCES.c.source_integration_id == result["integration_id"])).all()
        assert len(rows) == 1


@pytest.mark.parametrize("mutation", ["private", "cross_project", "source_identity", "stale_sequence"])
def test_publisher_rechecks_pinned_scope_and_machine_source(tmp_path, mutation):
    value = prepare(tmp_path)
    publish = value.integration.publisher.publish

    def changed(connection, **kwargs):
        if mutation == "private":
            connection.execute(PROJECT_RESOURCES.update().values(propagation="team_private"))
        elif mutation == "cross_project":
            connection.execute(INTEGRATION_RUNS.update().values(project_id="unrelated-project"))
        elif mutation == "source_identity":
            connection.execute(INTEGRATION_RUNS.update().values(executed_as="team-agent:team-b"))
        else:
            connection.execute(INTEGRATION_RUNS.update().values(based_on_event_sequence=0))
        return publish(connection, **kwargs)

    value.integration.publisher.publish = changed
    with pytest.raises(GovernanceConflictError):
        execute(value)
    assert_rolled_back(value)
