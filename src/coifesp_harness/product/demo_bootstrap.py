"""Idempotent Local-mode demo bootstrap.

Local profiles (``lead``/``contributor``/``reviewer``) are in-memory
identities; this module seeds the matching persistent product accounts,
teams, team relations, one example project and the unique per-account
conversations so the product workspace can be demonstrated end to end.
Repeated startup never duplicates data.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.engine import Engine

from ..errors import GovernanceConflictError, PolicyDenied, ResourceNotFound
from .models import ProjectTeamKind, TeamAccountRole
from .repository import ACCOUNTS, CONTACTS, PROJECTS, PROJECT_TEAMS, TEAMS
from .service import ProductAccountService, ProjectDirectoryService
from .workspace import ProjectWorkspaceService

DEMO_TEAMS = (
    ("team-product", "product", "产品团队"),
    ("team-engineering", "engineering", "工程团队"),
    ("team-quality", "quality", "质量团队"),
)

DEMO_ACCOUNTS = (
    ("lead-lin", "lead-lin", "林澈 · 项目主导", "team-product", TeamAccountRole.ADMIN),
    ("contributor-zhou", "contributor-zhou", "周宁 · 开发配合", "team-engineering",
     TeamAccountRole.MEMBER),
    ("reviewer-su", "reviewer-su", "苏禾 · 独立评审", "team-quality", TeamAccountRole.MEMBER),
)

DEMO_PROJECT_ID = "project-coifesp-demo"
DEMO_PROJECT_NAME = "COIFESP 多团队协作演示"
DEMO_PROJECT_DESCRIPTION = (
    "演示项目：产品、工程与质量团队通过各自的项目 Agent 完成规划、协作、开发与验收。"
)


@dataclass(frozen=True, slots=True)
class LocalDemoSummary:
    project_id: str
    conversation_ids: tuple[str, ...]
    team_ids: tuple[str, ...]
    created: bool


def ensure_local_demo(*, engine: Engine) -> LocalDemoSummary:
    accounts = ProductAccountService(engine)
    directory = ProjectDirectoryService(engine)
    workspace = ProjectWorkspaceService(engine)

    for team_id, handle, name in DEMO_TEAMS:
        try:
            accounts.register_team(team_id=team_id, team_handle=handle, team_name=name)
        except GovernanceConflictError:
            pass

    for account_id, username, display_name, team_id, role in DEMO_ACCOUNTS:
        accounts.ensure_active_account(
            account_id=account_id,
            username=username,
            display_name=display_name,
            email=f"{account_id}@demo.invalid",
            team_id=team_id,
            team_role=role,
        )

    _ensure_relation(accounts, sender_actor="lead-lin", recipient_handle="engineering")
    _ensure_relation(accounts, sender_actor="lead-lin", recipient_handle="quality")

    project_exists = _row_exists(engine, PROJECTS, PROJECTS.c.project_id == DEMO_PROJECT_ID)
    if not project_exists:
        directory.create_project(
            project_id=DEMO_PROJECT_ID,
            name=DEMO_PROJECT_NAME,
            description=DEMO_PROJECT_DESCRIPTION,
            actor_id="lead-lin",
            owner_assignment_name="产品统筹",
            owner_kind=ProjectTeamKind.PRODUCT,
        )
    _ensure_project_team(directory, project_id=DEMO_PROJECT_ID, team_id="team-engineering",
        name="工程交付", kind=ProjectTeamKind.ENGINEERING)
    _ensure_project_team(directory, project_id=DEMO_PROJECT_ID, team_id="team-quality",
        name="质量评审", kind=ProjectTeamKind.QUALITY)

    conversation_ids = tuple(
        workspace.ensure_conversation(project_id=DEMO_PROJECT_ID, actor_id=account[0]).conversation_id
        for account in DEMO_ACCOUNTS
    )
    return LocalDemoSummary(
        project_id=DEMO_PROJECT_ID,
        conversation_ids=conversation_ids,
        team_ids=tuple(item[0] for item in DEMO_TEAMS),
        created=not project_exists,
    )


def _ensure_relation(
    accounts: ProductAccountService,
    *,
    sender_actor: str,
    recipient_handle: str,
) -> None:
    with accounts.engine.connect() as connection:
        sender_team = accounts.get_account(sender_actor).team_id
        recipient_row = (
            connection.execute(
                select(TEAMS.c.team_id).where(
                    TEAMS.c.handle_key == recipient_handle.casefold()
                )
            ).scalar_one_or_none()
        )
    if recipient_row is None:
        return
    low, high = sorted((sender_team, recipient_row))
    with accounts.engine.connect() as connection:
        already = (
            connection.execute(
                select(CONTACTS.c.team_low).where(
                    (CONTACTS.c.team_low == low) & (CONTACTS.c.team_high == high)
                )
            ).scalar_one_or_none()
            is not None
        )
    if already:
        return
    request_id = f"rel-demo-{low}-{high}"
    try:
        accounts.send_team_relation_request(
            request_id=request_id,
            actor_id=sender_actor,
            recipient_team_handle=recipient_handle,
            message="本地演示项目协作关系",
        )
    except GovernanceConflictError:
        return
    admin_id = _team_admin_account_id(accounts, recipient_handle)
    if admin_id is None:
        return
    try:
        accounts.decide_team_relation_request(
            request_id=request_id, actor_id=admin_id, accept=True
        )
    except (GovernanceConflictError, PolicyDenied, ResourceNotFound):
        pass


def _ensure_project_team(
    directory: ProjectDirectoryService,
    *,
    project_id: str,
    team_id: str,
    name: str,
    kind: ProjectTeamKind,
) -> None:
    with directory.engine.connect() as connection:
        present = (
            connection.execute(
                select(PROJECT_TEAMS.c.team_id).where(
                    (PROJECT_TEAMS.c.project_id == project_id)
                    & (PROJECT_TEAMS.c.team_id == team_id)
                )
            ).scalar_one_or_none()
            is not None
        )
    if present:
        return
    try:
        directory.add_team(
            project_id=project_id,
            team_id=team_id,
            name=name,
            kind=kind,
            actor_id="lead-lin",
        )
    except (GovernanceConflictError, PolicyDenied, ResourceNotFound):
        pass


def _team_admin_account_id(accounts: ProductAccountService, handle: str) -> str | None:
    username = f"{handle}-admin"
    with accounts.engine.connect() as connection:
        return connection.execute(
            select(ACCOUNTS.c.account_id).where(
                ACCOUNTS.c.username_key == username.casefold()
            )
        ).scalar_one_or_none()


def _row_exists(engine: Engine, table, condition) -> bool:
    with engine.connect() as connection:
        return connection.execute(select(table).where(condition)).first() is not None