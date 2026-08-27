import hashlib
import json

import pytest

from coifesp_harness.tools import (
    ApprovalReviewField,
    ApprovalReviewPolicy,
    ApprovalReviewProjector,
    ReviewDisclosure,
)


def test_tool_owned_projection_discloses_only_allowlisted_fields() -> None:
    arguments = {
        "destination": {"system": "crm", "tenant": "customer-42"},
        "records": [{"id": 1}, {"id": 2}],
        "api_key": "must-never-appear",
    }
    projection = ApprovalReviewProjector().project(
        tool_name="customer_export",
        arguments=arguments,
        policy=ApprovalReviewPolicy(
            fields=(
                ApprovalReviewField(
                    "destination",
                    "/destination/system",
                    ReviewDisclosure.VALUE,
                ),
                ApprovalReviewField("record_count", "/records", ReviewDisclosure.COUNT),
                ApprovalReviewField("tenant_hash", "/destination/tenant", ReviewDisclosure.HASH),
                ApprovalReviewField("credential", "/api_key", ReviewDisclosure.REDACTED),
            )
        ),
    )
    encoded = json.dumps(projection, sort_keys=True)
    assert projection["schema"] == "coifesp.approval-review.v1"
    assert projection["fields"][0]["value"] == "crm"
    assert projection["fields"][1]["count"] == 2
    assert (
        projection["fields"][2]["value_digest"]
        == hashlib.sha256(json.dumps("customer-42").encode()).hexdigest()
    )
    assert projection["fields"][3]["value"] == "[REDACTED]"
    assert "must-never-appear" not in encoded


def test_projection_rejects_unresolvable_or_duplicate_review_fields() -> None:
    with pytest.raises(ValueError, match="does not resolve"):
        ApprovalReviewProjector().project(
            tool_name="deploy",
            arguments={"environment": "production"},
            policy=ApprovalReviewPolicy(
                fields=(
                    ApprovalReviewField(
                        "region",
                        "/region",
                        ReviewDisclosure.VALUE,
                    ),
                )
            ),
        )
    with pytest.raises(ValueError, match="unique"):
        ApprovalReviewPolicy(
            fields=(
                ApprovalReviewField("same", "/one", ReviewDisclosure.VALUE),
                ApprovalReviewField("same", "/two", ReviewDisclosure.VALUE),
            )
        )


def test_projection_allows_distinct_hash_and_count_views_without_plaintext() -> None:
    projection = ApprovalReviewProjector().project(
        tool_name="office.send_message",
        arguments={"text": "private message"},
        policy=ApprovalReviewPolicy(
            fields=(
                ApprovalReviewField("text_hash", "/text", ReviewDisclosure.HASH),
                ApprovalReviewField("text_chars", "/text", ReviewDisclosure.COUNT),
            )
        ),
    )
    encoded = json.dumps(projection)
    assert projection["fields"][1]["count"] == len("private message")
    assert "private message" not in encoded
