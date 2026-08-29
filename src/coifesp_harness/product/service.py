from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, func, insert, or_, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from ..errors import (
    AuthenticationError,
    GovernanceConflictError,
    PolicyDenied,
    ResourceNotFound,
)
from .models import (
    TASK_PRIORITY_RANK,
    TASK_SCHEDULE_DUE_MAX_DAYS,
    TASK_TERMINAL_STATUSES,
    Account,
    AccountRegistration,
    AccountRegistrationStatus,
    AccountSession,
    CollaborationActionDraft,
    CollaborationDraftKind,
    CollaborationDraftStatus,
    CollaborationInbox,
    CollaborationInboxAction,
    CollaborationInboxActivity,
    ContactRequestStatus,
    DataPropagation,
    InboxAgentMode,
    InboxAgentRun,
    Project,
    ProjectActivity,
    ProjectAgentMode,
    ProjectAgentRun,
    ProjectDetail,
    ProjectMembership,
    ProjectMessage,
    ProjectNotificationSummary,
    ProjectResource,
    ProjectRole,
    ProjectTeam,
    ProjectTeamKind,
    ProjectTopic,
    ProjectTopicContribution,
    ProjectTopicStatus,
    ResourceAccess,
    ResourceAction,
    TaskPriority,
    TaskScheduleProposal,
    TaskScheduleProposalStatus,
    Team,
    TeamAccountRole,
    TeamBootstrapAdmin,
    TeamDirectoryEntry,
    TeamDirectoryPage,
    TeamRelation,
    TeamRelationRequest,
    TeamRelationshipState,
    TeamTask,
    TeamTaskStatus,
    compute_team_task_schedule,
    task_is_overdue,
)
from .repository import (
    ACCOUNT_REGISTRATIONS,
    ACCOUNT_SESSIONS,
    ACCOUNTS,
    ALL_PRODUCT_TABLES,
    COLLABORATION_ACTION_DRAFTS,
    CONTACT_REQUESTS,
    CONTACTS,
    INBOX_AGENT_RUNS,
    PRODUCT_METADATA,
    PROJECT_ACTIVITIES,
    PROJECT_ACTIVITY_CURSORS,
    PROJECT_AGENT_RUNS,
    PROJECT_MEMBERSHIPS,
    PROJECT_MESSAGES,
    PROJECT_RESOURCES,
    PROJECT_TEAMS,
    PROJECT_TOPIC_CONTRIBUTIONS,
    PROJECT_TOPICS,
    PROJECTS,
    RESOURCE_SHARES,
    TASK_SCHEDULE_PROPOSALS,
    TEAM_TASKS,
    TEAMS,
)

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_HANDLE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,63}$")


def _validate_due_at(due_at: datetime, now: datetime) -> None:
    if due_at.tzinfo is None:
        raise ValueError("task due time must carry an explicit timezone")
    if due_at <= now:
        raise ValueError("task due time must be in the future")
    if due_at > now + timedelta(days=TASK_SCHEDULE_DUE_MAX_DAYS):
        raise ValueError("task due time exceeds the maximum scheduling horizon")


class ProductAccountService:
    def __init__(self, engine: Engine, *, session_hours: int = 24 * 7) -> None:
        self.engine = engine
        self.session_hours = session_hours

    def create_schema(self) -> None:
        PRODUCT_METADATA.create_all(self.engine, tables=list(ALL_PRODUCT_TABLES))

    def register_team(
        self, *, team_id: str, team_handle: str, team_name: str
    ) -> tuple[Team, TeamBootstrapAdmin]:
        self._identifier(team_id)
        if not _HANDLE.fullmatch(team_handle) or not team_name.strip():
            raise ValueError("team profile is invalid")
        now = datetime.now(UTC)
        admin_username = f"{team_handle}-admin"
        admin_account_id = f"acct-{secrets.token_hex(12)}"
        initial_password = secrets.token_urlsafe(24)
        try:
            with self.engine.begin() as connection:
                connection.execute(
                    insert(TEAMS).values(
                        team_id=team_id,
                        handle=team_handle,
                        handle_key=team_handle.casefold(),
                        name=team_name.strip(),
                        created_at=now,
                    )
                )
                values = self._account_values(
                    admin_account_id,
                    admin_username,
                    f"{team_name.strip()} 管理员",
                    f"{admin_account_id}@bootstrap.invalid",
                    initial_password,
                    team_id,
                    TeamAccountRole.OWNER,
                    now,
                    AccountRegistrationStatus.ACTIVE,
                    must_change_password=True,
                )
                connection.execute(
                    insert(ACCOUNT_REGISTRATIONS).values(
                        account_id=admin_account_id,
                        username=admin_username,
                        username_key=admin_username.casefold(),
                        display_name=values["display_name"],
                        email=values["email"],
                        email_key=values["email_key"],
                        password_hash=values["password_hash"],
                        team_id=team_id,
                        status=AccountRegistrationStatus.ACTIVE.value,
                        created_at=now,
                        decided_by=None,
                        decided_at=now,
                    )
                )
                connection.execute(insert(ACCOUNTS).values(**values))
        except Exception as exc:
            raise GovernanceConflictError("team handle or ID already exists") from exc
        account = self._account(values)
        return (
            Team(team_id, team_handle, team_name.strip(), now),
            TeamBootstrapAdmin(admin_username, initial_password, account),
        )

    def ensure_active_account(
        self,
        *,
        account_id: str,
        username: str,
        display_name: str,
        email: str,
        team_id: str,
        team_role: TeamAccountRole = TeamAccountRole.MEMBER,
    ) -> Account:
        """Idempotently create an activated account for local demos and seeding.

        Re-running returns the existing account; the unique username/email
        constraints make duplicate creation impossible.
        """
        self._identifier(account_id)
        if not _HANDLE.fullmatch(username) or not display_name.strip() or "@" not in email:
            raise ValueError("account profile is invalid")
        now = datetime.now(UTC)
        try:
            with self.engine.begin() as connection:
                values = self._account_values(
                    account_id,
                    username,
                    display_name.strip(),
                    email,
                    secrets.token_urlsafe(24),
                    team_id,
                    team_role,
                    now,
                    AccountRegistrationStatus.ACTIVE,
                    must_change_password=False,
                )
                connection.execute(insert(ACCOUNTS).values(**values))
            return self.get_account(account_id)
        except IntegrityError:
            return self.get_account(account_id)

    def request_account_registration(
        self,
        *,
        account_id: str,
        username: str,
        display_name: str,
        email: str,
        password: str,
        team_id: str,
    ) -> AccountRegistration:
        self._validate_registration(account_id, username, display_name, email, password)
        now = datetime.now(UTC)
        with self.engine.begin() as connection:
            team_exists = connection.execute(
                select(TEAMS.c.team_id).where(TEAMS.c.team_id == team_id)
            ).scalar_one_or_none()
            if team_exists is None:
                raise ResourceNotFound("team is unavailable")
            existing_account = connection.execute(
                select(ACCOUNTS.c.account_id).where(
                    or_(
                        ACCOUNTS.c.username_key == username.casefold(),
                        ACCOUNTS.c.email_key == email.casefold(),
                    )
                )
            ).scalar_one_or_none()
            if existing_account is not None:
                raise GovernanceConflictError("username or email already belongs to an account")
            values = dict(
                account_id=account_id,
                username=username,
                username_key=username.casefold(),
                display_name=display_name.strip(),
                email=email,
                email_key=email.casefold(),
                password_hash=self._password_hash(password),
                team_id=team_id,
                status=AccountRegistrationStatus.PENDING.value,
                created_at=now,
                decided_by=None,
                decided_at=None,
            )
            try:
                connection.execute(insert(ACCOUNT_REGISTRATIONS).values(**values))
            except Exception as exc:
                raise GovernanceConflictError("username, email, or account already exists") from exc
        return self._registration(values)

    def decide_account_registration(
        self, *, actor_id: str, account_id: str, accept: bool
    ) -> AccountRegistration:
        with self.engine.begin() as connection:
            actor = self._account_row(connection, actor_id)
            if actor[
                "registration_status"
            ] != AccountRegistrationStatus.ACTIVE.value or TeamAccountRole(
                actor["team_role"]
            ) not in {
                TeamAccountRole.OWNER,
                TeamAccountRole.ADMIN,
            }:
                raise PolicyDenied("only active team administrators may review registrations")
            candidate = (
                connection.execute(
                    select(ACCOUNT_REGISTRATIONS)
                    .where(ACCOUNT_REGISTRATIONS.c.account_id == account_id)
                    .with_for_update()
                )
                .mappings()
                .one_or_none()
            )
            if candidate is None or candidate["team_id"] != actor["team_id"]:
                raise ResourceNotFound("account registration is unavailable")
            if candidate["status"] != AccountRegistrationStatus.PENDING.value:
                raise GovernanceConflictError("account registration is no longer pending")
            status = (
                AccountRegistrationStatus.ACTIVE if accept else AccountRegistrationStatus.REJECTED
            )
            connection.execute(
                update(ACCOUNT_REGISTRATIONS)
                .where(ACCOUNT_REGISTRATIONS.c.account_id == account_id)
                .values(status=status.value, decided_by=actor_id, decided_at=datetime.now(UTC))
            )
            if accept:
                connection.execute(
                    insert(ACCOUNTS).values(
                        account_id=candidate["account_id"],
                        username=candidate["username"],
                        username_key=candidate["username_key"],
                        display_name=candidate["display_name"],
                        email=candidate["email"],
                        email_key=candidate["email_key"],
                        password_hash=candidate["password_hash"],
                        team_id=candidate["team_id"],
                        team_role=TeamAccountRole.MEMBER.value,
                        registration_status=AccountRegistrationStatus.ACTIVE.value,
                        must_change_password=False,
                        enabled=True,
                        created_at=candidate["created_at"],
                    )
                )
            result = dict(candidate)
            result["status"] = status.value
        return self._registration(result)

    def list_pending_registrations(self, actor_id: str) -> list[AccountRegistration]:
        with self.engine.connect() as connection:
            actor = self._account_row(connection, actor_id)
            if actor[
                "registration_status"
            ] != AccountRegistrationStatus.ACTIVE.value or TeamAccountRole(
                actor["team_role"]
            ) not in {
                TeamAccountRole.OWNER,
                TeamAccountRole.ADMIN,
            }:
                raise PolicyDenied("only active team administrators may review registrations")
            rows = (
                connection.execute(
                    select(ACCOUNT_REGISTRATIONS)
                    .where(
                        and_(
                            ACCOUNT_REGISTRATIONS.c.team_id == actor["team_id"],
                            ACCOUNT_REGISTRATIONS.c.status
                            == AccountRegistrationStatus.PENDING.value,
                        )
                    )
                    .order_by(ACCOUNT_REGISTRATIONS.c.created_at)
                )
                .mappings()
                .all()
            )
        return [self._registration(row) for row in rows]

    def login(self, *, login: str, password: str) -> AccountSession:
        with self.engine.begin() as connection:
            row = (
                connection.execute(
                    select(ACCOUNTS).where(
                        or_(
                            ACCOUNTS.c.username_key == login.casefold(),
                            ACCOUNTS.c.email_key == login.casefold(),
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            if (
                row is None
                or not row["enabled"]
                or row["must_change_password"]
                or row["registration_status"] != AccountRegistrationStatus.ACTIVE.value
                or not self._verify_password(password, row["password_hash"])
            ):
                raise AuthenticationError("invalid username or password")
            token = secrets.token_urlsafe(32)
            now = datetime.now(UTC)
            expires = now + timedelta(hours=self.session_hours)
            connection.execute(
                insert(ACCOUNT_SESSIONS).values(
                    session_hash=self._token_hash(token),
                    account_id=row["account_id"],
                    created_at=now,
                    expires_at=expires,
                    revoked_at=None,
                )
            )
        return AccountSession(token, self._account(row), expires)

    def change_initial_password(
        self, *, login: str, current_password: str, new_password: str
    ) -> None:
        if len(new_password) < 12 or len(new_password.encode("utf-8")) > 1024:
            raise ValueError("password must contain at least 12 characters")
        with self.engine.begin() as connection:
            row = (
                connection.execute(
                    select(ACCOUNTS)
                    .where(
                        or_(
                            ACCOUNTS.c.username_key == login.casefold(),
                            ACCOUNTS.c.email_key == login.casefold(),
                        )
                    )
                    .with_for_update()
                )
                .mappings()
                .one_or_none()
            )
            if (
                row is None
                or not row["enabled"]
                or not row["must_change_password"]
                or not self._verify_password(current_password, row["password_hash"])
            ):
                raise AuthenticationError("initial credential is invalid")
            connection.execute(
                update(ACCOUNTS)
                .where(ACCOUNTS.c.account_id == row["account_id"])
                .values(password_hash=self._password_hash(new_password), must_change_password=False)
            )

    def authenticate(self, token: str) -> Account:
        return self.authenticate_session(token).account

    def authenticate_session(self, token: str) -> AccountSession:
        now = datetime.now(UTC)
        with self.engine.connect() as connection:
            row = (
                connection.execute(
                    select(ACCOUNTS, ACCOUNT_SESSIONS.c.expires_at)
                    .select_from(
                        ACCOUNT_SESSIONS.join(
                            ACCOUNTS, ACCOUNT_SESSIONS.c.account_id == ACCOUNTS.c.account_id
                        )
                    )
                    .where(
                        and_(
                            ACCOUNT_SESSIONS.c.session_hash == self._token_hash(token),
                            ACCOUNT_SESSIONS.c.revoked_at.is_(None),
                            ACCOUNT_SESSIONS.c.expires_at > now,
                            ACCOUNTS.c.enabled.is_(True),
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            raise AuthenticationError("invalid or expired session")
        return AccountSession(token, self._account(row), self._aware(row["expires_at"]))

    def get_account(self, account_id: str) -> Account:
        with self.engine.connect() as connection:
            row = self._account_row(connection, account_id)
        return self._account(row)

    def get_team(self, team_id: str) -> Team:
        with self.engine.connect() as connection:
            row = (
                connection.execute(select(TEAMS).where(TEAMS.c.team_id == team_id))
                .mappings()
                .one_or_none()
            )
        if row is None:
            raise ResourceNotFound("team is unavailable")
        return Team(row["team_id"], row["handle"], row["name"], self._aware(row["created_at"]))

    def list_teams(self) -> list[Team]:
        with self.engine.connect() as connection:
            rows = connection.execute(select(TEAMS).order_by(TEAMS.c.name)).mappings().all()
        return [
            Team(row["team_id"], row["handle"], row["name"], self._aware(row["created_at"]))
            for row in rows
        ]

    def search_team_directory(
        self,
        *,
        actor_id: str,
        query: str = "",
        after_handle: str | None = None,
        limit: int = 50,
    ) -> TeamDirectoryPage:
        clean_query = query.strip()
        if len(clean_query.encode("utf-8")) > 256:
            raise ValueError("team directory query is too long")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("team directory limit is invalid")
        if after_handle is not None and not _HANDLE.fullmatch(after_handle):
            raise ValueError("team directory cursor is invalid")
        with self.engine.connect() as connection:
            actor = self._account_row(connection, actor_id)
            team_id = actor["team_id"]
            filters = [TEAMS.c.team_id != team_id]
            if clean_query:
                escaped = (
                    clean_query.casefold()
                    .replace("\\", "\\\\")
                    .replace("%", "\\%")
                    .replace("_", "\\_")
                )
                pattern = f"%{escaped}%"
                filters.append(
                    or_(
                        TEAMS.c.handle_key.like(pattern, escape="\\"),
                        func.lower(TEAMS.c.name).like(pattern, escape="\\"),
                    )
                )
            if after_handle is not None:
                filters.append(TEAMS.c.handle_key > after_handle.casefold())
            rows = (
                connection.execute(
                    select(TEAMS)
                    .where(and_(*filters))
                    .order_by(TEAMS.c.handle_key)
                    .limit(limit + 1)
                )
                .mappings()
                .all()
            )
            relation_rows = (
                connection.execute(
                    select(CONTACTS).where(
                        or_(CONTACTS.c.team_low == team_id, CONTACTS.c.team_high == team_id)
                    )
                )
                .mappings()
                .all()
            )
            connected = {
                row["team_high"] if row["team_low"] == team_id else row["team_low"]
                for row in relation_rows
            }
            pending_rows = (
                connection.execute(
                    select(CONTACT_REQUESTS).where(
                        and_(
                            CONTACT_REQUESTS.c.status == ContactRequestStatus.PENDING.value,
                            or_(
                                CONTACT_REQUESTS.c.sender_team_id == team_id,
                                CONTACT_REQUESTS.c.recipient_team_id == team_id,
                            ),
                        )
                    )
                )
                .mappings()
                .all()
            )
            pending = {
                (
                    row["recipient_team_id"]
                    if row["sender_team_id"] == team_id
                    else row["sender_team_id"]
                ): (
                    TeamRelationshipState.PENDING_OUTGOING
                    if row["sender_team_id"] == team_id
                    else TeamRelationshipState.PENDING_INCOMING
                )
                for row in pending_rows
            }
        page_rows, has_more = rows[:limit], len(rows) > limit
        items = tuple(
            TeamDirectoryEntry(
                Team(
                    row["team_id"],
                    row["handle"],
                    row["name"],
                    self._aware(row["created_at"]),
                ),
                (
                    TeamRelationshipState.CONNECTED
                    if row["team_id"] in connected
                    else pending.get(row["team_id"], TeamRelationshipState.AVAILABLE)
                ),
            )
            for row in page_rows
        )
        next_cursor = page_rows[-1]["handle"] if has_more else None
        return TeamDirectoryPage(items, next_cursor)

    def list_team_accounts(self, actor_id: str) -> list[Account]:
        """Return active accounts in the actor's own team for internal assignment.

        Account identities never cross the team boundary through this API. External
        collaboration continues to address teams only.
        """
        with self.engine.connect() as connection:
            actor = self._account_row(connection, actor_id)
            rows = (
                connection.execute(
                    select(ACCOUNTS)
                    .where(
                        and_(
                            ACCOUNTS.c.team_id == actor["team_id"],
                            ACCOUNTS.c.registration_status
                            == AccountRegistrationStatus.ACTIVE.value,
                            ACCOUNTS.c.enabled.is_(True),
                        )
                    )
                    .order_by(ACCOUNTS.c.display_name, ACCOUNTS.c.account_id)
                )
                .mappings()
                .all()
            )
        return [self._account(row) for row in rows]

    def logout(self, token: str) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                update(ACCOUNT_SESSIONS)
                .where(ACCOUNT_SESSIONS.c.session_hash == self._token_hash(token))
                .values(revoked_at=datetime.now(UTC))
            )

    def renew_session(self, token: str) -> AccountSession:
        """Atomically rotate one still-valid session and revoke the old token.

        The caller must already have proven ownership of ``token`` through the
        bearer verifier. Renewal never extends an expired or revoked session,
        and every renewal issues a fresh opaque token so the old one stops
        working immediately.
        """
        now = datetime.now(UTC)
        with self.engine.begin() as connection:
            row = (
                connection.execute(
                    select(ACCOUNTS, ACCOUNT_SESSIONS.c.expires_at)
                    .select_from(
                        ACCOUNT_SESSIONS.join(
                            ACCOUNTS, ACCOUNT_SESSIONS.c.account_id == ACCOUNTS.c.account_id
                        )
                    )
                    .where(
                        and_(
                            ACCOUNT_SESSIONS.c.session_hash == self._token_hash(token),
                            ACCOUNT_SESSIONS.c.revoked_at.is_(None),
                            ACCOUNT_SESSIONS.c.expires_at > now,
                            ACCOUNTS.c.enabled.is_(True),
                        )
                    )
                    .with_for_update()
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise AuthenticationError("invalid or expired session")
            new_token = secrets.token_urlsafe(32)
            expires = now + timedelta(hours=self.session_hours)
            connection.execute(
                update(ACCOUNT_SESSIONS)
                .where(ACCOUNT_SESSIONS.c.session_hash == self._token_hash(token))
                .values(revoked_at=now)
            )
            connection.execute(
                insert(ACCOUNT_SESSIONS).values(
                    session_hash=self._token_hash(new_token),
                    account_id=row["account_id"],
                    created_at=now,
                    expires_at=expires,
                    revoked_at=None,
                )
            )
        return AccountSession(new_token, self._account(row), expires)

    def revoke_all_sessions(self, account_id: str) -> None:
        """Global logout: revoke every live session of the account at once."""
        with self.engine.begin() as connection:
            connection.execute(
                update(ACCOUNT_SESSIONS)
                .where(
                    and_(
                        ACCOUNT_SESSIONS.c.account_id == account_id,
                        ACCOUNT_SESSIONS.c.revoked_at.is_(None),
                    )
                )
                .values(revoked_at=datetime.now(UTC))
            )

    def send_team_relation_request(
        self, *, request_id: str, actor_id: str, recipient_team_handle: str, message: str = ""
    ) -> TeamRelationRequest:
        self._identifier(request_id)
        now = datetime.now(UTC)
        with self.engine.begin() as connection:
            actor = self._account_row(connection, actor_id)
            if TeamAccountRole(actor["team_role"]) not in {
                TeamAccountRole.OWNER,
                TeamAccountRole.ADMIN,
            }:
                raise PolicyDenied("only team owners and administrators may manage team relations")
            recipient = (
                connection.execute(
                    select(TEAMS).where(TEAMS.c.handle_key == recipient_team_handle.casefold())
                )
                .mappings()
                .one_or_none()
            )
            if recipient is None:
                raise ResourceNotFound("team is unavailable")
            sender_team, recipient_team = actor["team_id"], recipient["team_id"]
            if sender_team == recipient_team or self._are_related(
                connection, sender_team, recipient_team
            ):
                raise GovernanceConflictError("teams are already connected")
            pending = connection.execute(
                select(CONTACT_REQUESTS.c.request_id).where(
                    and_(
                        CONTACT_REQUESTS.c.status == ContactRequestStatus.PENDING.value,
                        or_(
                            and_(
                                CONTACT_REQUESTS.c.sender_team_id == sender_team,
                                CONTACT_REQUESTS.c.recipient_team_id == recipient_team,
                            ),
                            and_(
                                CONTACT_REQUESTS.c.sender_team_id == recipient_team,
                                CONTACT_REQUESTS.c.recipient_team_id == sender_team,
                            ),
                        ),
                    )
                )
            ).scalar_one_or_none()
            if pending:
                raise GovernanceConflictError("a team relation request is already pending")
            connection.execute(
                insert(CONTACT_REQUESTS).values(
                    request_id=request_id,
                    sender_team_id=sender_team,
                    recipient_team_id=recipient_team,
                    requested_by=actor_id,
                    message=message.strip(),
                    status=ContactRequestStatus.PENDING.value,
                    created_at=now,
                    decided_at=None,
                )
            )
        return TeamRelationRequest(
            request_id,
            sender_team,
            recipient_team,
            actor_id,
            message.strip(),
            ContactRequestStatus.PENDING,
            now,
        )

    def decide_team_relation_request(
        self, *, request_id: str, actor_id: str, accept: bool
    ) -> TeamRelationRequest:
        now = datetime.now(UTC)
        with self.engine.begin() as connection:
            actor = self._account_row(connection, actor_id)
            if TeamAccountRole(actor["team_role"]) not in {
                TeamAccountRole.OWNER,
                TeamAccountRole.ADMIN,
            }:
                raise PolicyDenied("only team owners and administrators may manage team relations")
            row = (
                connection.execute(
                    select(CONTACT_REQUESTS)
                    .where(CONTACT_REQUESTS.c.request_id == request_id)
                    .with_for_update()
                )
                .mappings()
                .one_or_none()
            )
            if row is None or row["recipient_team_id"] != actor["team_id"]:
                raise ResourceNotFound("team relation request is unavailable")
            if row["status"] != ContactRequestStatus.PENDING.value:
                raise GovernanceConflictError("team relation request is no longer pending")
            status = ContactRequestStatus.ACCEPTED if accept else ContactRequestStatus.DECLINED
            connection.execute(
                update(CONTACT_REQUESTS)
                .where(CONTACT_REQUESTS.c.request_id == request_id)
                .values(status=status.value, decided_at=now)
            )
            if accept:
                low, high = sorted((row["sender_team_id"], row["recipient_team_id"]))
                connection.execute(
                    insert(CONTACTS).values(
                        team_low=low, team_high=high, accepted_request_id=request_id, created_at=now
                    )
                )
        return TeamRelationRequest(
            request_id,
            row["sender_team_id"],
            row["recipient_team_id"],
            row["requested_by"],
            row["message"],
            status,
            self._aware(row["created_at"]),
            now,
        )

    def list_team_relation_requests(self, actor_id: str) -> list[TeamRelationRequest]:
        with self.engine.connect() as connection:
            actor = self._account_row(connection, actor_id)
            rows = (
                connection.execute(
                    select(CONTACT_REQUESTS)
                    .where(
                        or_(
                            CONTACT_REQUESTS.c.sender_team_id == actor["team_id"],
                            CONTACT_REQUESTS.c.recipient_team_id == actor["team_id"],
                        )
                    )
                    .order_by(CONTACT_REQUESTS.c.created_at.desc())
                )
                .mappings()
                .all()
            )
        return [
            TeamRelationRequest(
                row["request_id"],
                row["sender_team_id"],
                row["recipient_team_id"],
                row["requested_by"],
                row["message"],
                ContactRequestStatus(row["status"]),
                self._aware(row["created_at"]),
                self._aware(row["decided_at"]) if row["decided_at"] else None,
            )
            for row in rows
        ]

    def list_team_relations(self, actor_id: str) -> list[TeamRelation]:
        with self.engine.connect() as connection:
            actor = self._account_row(connection, actor_id)
            team_id = actor["team_id"]
            relations = (
                connection.execute(
                    select(CONTACTS).where(
                        or_(
                            CONTACTS.c.team_low == team_id,
                            CONTACTS.c.team_high == team_id,
                        )
                    )
                )
                .mappings()
                .all()
            )
            related_ids = [
                row["team_high"] if row["team_low"] == team_id else row["team_low"]
                for row in relations
            ]
            if not related_ids:
                return []
            teams = {
                row["team_id"]: row
                for row in connection.execute(select(TEAMS).where(TEAMS.c.team_id.in_(related_ids)))
                .mappings()
                .all()
            }
        created = {
            row["team_high"] if row["team_low"] == team_id else row["team_low"]: self._aware(
                row["created_at"]
            )
            for row in relations
        }
        return [
            TeamRelation(
                Team(row["team_id"], row["handle"], row["name"], self._aware(row["created_at"])),
                created[row["team_id"]],
            )
            for row in sorted(teams.values(), key=lambda item: item["name"])
        ]

    @staticmethod
    def _account_values(
        account_id,
        username,
        display_name,
        email,
        password,
        team_id,
        team_role,
        now,
        registration_status,
        must_change_password,
    ):
        return dict(
            account_id=account_id,
            username=username,
            username_key=username.casefold(),
            display_name=display_name.strip(),
            email=email,
            email_key=email.casefold(),
            password_hash=ProductAccountService._password_hash(password),
            team_id=team_id,
            team_role=team_role.value,
            registration_status=registration_status.value,
            must_change_password=must_change_password,
            enabled=registration_status is AccountRegistrationStatus.ACTIVE,
            created_at=now,
        )

    def _validate_registration(self, account_id, username, display_name, email, password):
        self._identifier(account_id)
        if not _HANDLE.fullmatch(username) or not display_name.strip() or "@" not in email:
            raise ValueError("account profile is invalid")
        if len(password) < 12 or len(password.encode("utf-8")) > 1024:
            raise ValueError("password must contain at least 12 characters")

    @staticmethod
    def _password_hash(password: str) -> str:
        salt = secrets.token_bytes(16)
        derived = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
        return (
            "scrypt$16384$8$1$"
            + base64.urlsafe_b64encode(salt).decode()
            + "$"
            + base64.urlsafe_b64encode(derived).decode()
        )

    @staticmethod
    def _verify_password(password: str, encoded: str) -> bool:
        try:
            algorithm, raw_n, raw_r, raw_p, raw_salt, raw_hash = encoded.split("$")
            if algorithm != "scrypt":
                return False
            salt, expected = base64.urlsafe_b64decode(raw_salt), base64.urlsafe_b64decode(raw_hash)
            actual = hashlib.scrypt(
                password.encode(),
                salt=salt,
                n=int(raw_n),
                r=int(raw_r),
                p=int(raw_p),
                dklen=len(expected),
            )
            return hmac.compare_digest(actual, expected)
        except (ValueError, TypeError):
            return False

    _token_hash = staticmethod(lambda token: hashlib.sha256(token.encode()).hexdigest())

    @staticmethod
    def _account(row) -> Account:
        return Account(
            row["account_id"],
            row["username"],
            row["display_name"],
            row["email"],
            row["team_id"],
            TeamAccountRole(row["team_role"]),
            AccountRegistrationStatus(row["registration_status"]),
            bool(row["must_change_password"]),
            ProductAccountService._aware(row["created_at"]),
        )

    @staticmethod
    def _registration(row) -> AccountRegistration:
        return AccountRegistration(
            row["account_id"],
            row["username"],
            row["display_name"],
            row["email"],
            row["team_id"],
            AccountRegistrationStatus(row["status"]),
            ProductAccountService._aware(row["created_at"]),
        )

    @staticmethod
    def _account_row(connection, account_id):
        row = (
            connection.execute(select(ACCOUNTS).where(ACCOUNTS.c.account_id == account_id))
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise ResourceNotFound("account is unavailable")
        return row

    @staticmethod
    def _are_related(connection, first: str, second: str) -> bool:
        low, high = sorted((first, second))
        return (
            connection.execute(
                select(CONTACTS.c.team_low).where(
                    and_(CONTACTS.c.team_low == low, CONTACTS.c.team_high == high)
                )
            ).scalar_one_or_none()
            is not None
        )

    @staticmethod
    def _identifier(value: str) -> None:
        if not isinstance(value, str) or _ID.fullmatch(value) is None:
            raise ValueError("identifier is invalid")

    @staticmethod
    def _aware(value: datetime) -> datetime:
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class ProjectDirectoryService:
    def __init__(self, engine: Engine, *, process_shadow=None) -> None:
        self.engine = engine
        self.process_shadow = process_shadow

    def create_project(
        self,
        *,
        project_id: str,
        name: str,
        description: str,
        actor_id: str,
        owner_assignment_name: str,
        owner_kind: ProjectTeamKind,
    ) -> Project:
        ProductAccountService._identifier(project_id)
        if not name.strip() or not owner_assignment_name.strip():
            raise ValueError("project name is required")
        now = datetime.now(UTC)
        with self.engine.begin() as connection:
            actor = ProductAccountService._account_row(connection, actor_id)
            if TeamAccountRole(actor["team_role"]) not in {
                TeamAccountRole.OWNER,
                TeamAccountRole.ADMIN,
            }:
                raise PolicyDenied("only team owners and administrators may start projects")
            connection.execute(
                insert(PROJECTS).values(
                    project_id=project_id,
                    name=name.strip(),
                    description=description.strip(),
                    owner_team_id=actor["team_id"],
                    created_by=actor_id,
                    created_at=now,
                )
            )
            connection.execute(
                insert(PROJECT_TEAMS).values(
                    project_id=project_id,
                    team_id=actor["team_id"],
                    name=owner_assignment_name.strip(),
                    kind=owner_kind.value,
                    assigned_by=actor_id,
                    created_at=now,
                )
            )
            if self.process_shadow is not None:
                self.process_shadow.on_project_created(
                    connection,
                    project_id=project_id,
                    actor_id=actor_id,
                    description=description.strip(),
                )
        return Project(
            project_id, name.strip(), description.strip(), actor["team_id"], actor_id, now
        )

    def add_team(
        self, *, project_id: str, team_id: str, name: str, kind: ProjectTeamKind, actor_id: str
    ) -> ProjectTeam:
        now = datetime.now(UTC)
        with self.engine.begin() as connection:
            actor = ProductAccountService._account_row(connection, actor_id)
            owner = connection.execute(
                select(PROJECTS.c.owner_team_id).where(PROJECTS.c.project_id == project_id)
            ).scalar_one_or_none()
            if owner != actor["team_id"] or TeamAccountRole(actor["team_role"]) not in {
                TeamAccountRole.OWNER,
                TeamAccountRole.ADMIN,
            }:
                raise PolicyDenied("only the initiating team may compose project teams")
            if not ProductAccountService._are_related(connection, actor["team_id"], team_id):
                raise PolicyDenied("project teams must have an accepted team relation")
            connection.execute(
                insert(PROJECT_TEAMS).values(
                    project_id=project_id,
                    team_id=team_id,
                    name=name.strip(),
                    kind=kind.value,
                    assigned_by=actor_id,
                    created_at=now,
                )
            )
        return ProjectTeam(team_id, project_id, name.strip(), kind, actor_id)

    def list_projects(self, actor_id: str) -> list[Project]:
        with self.engine.connect() as connection:
            actor = ProductAccountService._account_row(connection, actor_id)
            rows = (
                connection.execute(
                    select(PROJECTS)
                    .where(
                        PROJECTS.c.project_id.in_(
                            select(PROJECT_TEAMS.c.project_id).where(
                                PROJECT_TEAMS.c.team_id == actor["team_id"]
                            )
                        )
                    )
                    .order_by(PROJECTS.c.created_at.desc())
                )
                .mappings()
                .all()
            )
        return [self._project(row) for row in rows]

    def get_project(self, *, project_id: str, actor_id: str) -> ProjectDetail:
        with self.engine.connect() as connection:
            actor = ProductAccountService._account_row(connection, actor_id)
            visible = connection.execute(
                select(PROJECT_TEAMS.c.team_id).where(
                    and_(
                        PROJECT_TEAMS.c.project_id == project_id,
                        PROJECT_TEAMS.c.team_id == actor["team_id"],
                    )
                )
            ).scalar_one_or_none()
            if visible is None:
                raise ResourceNotFound("project is unavailable")
            project = (
                connection.execute(select(PROJECTS).where(PROJECTS.c.project_id == project_id))
                .mappings()
                .one()
            )
            teams = (
                connection.execute(
                    select(PROJECT_TEAMS)
                    .where(PROJECT_TEAMS.c.project_id == project_id)
                    .order_by(PROJECT_TEAMS.c.name)
                )
                .mappings()
                .all()
            )
        return ProjectDetail(
            self._project(project),
            tuple(
                ProjectTeam(
                    row["team_id"],
                    row["project_id"],
                    row["name"],
                    ProjectTeamKind(row["kind"]),
                    row["assigned_by"],
                )
                for row in teams
            ),
        )

    @staticmethod
    def _project(row) -> Project:
        return Project(
            row["project_id"],
            row["name"],
            row["description"],
            row["owner_team_id"],
            row["created_by"],
            ProductAccountService._aware(row["created_at"]),
        )


class ProjectDataPolicy:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def decide(
        self,
        *,
        account_id: str,
        resource_id: str,
        action: ResourceAction,
        project_id: str | None = None,
    ) -> ResourceAccess:
        with self.engine.connect() as connection:
            account = ProductAccountService._account_row(connection, account_id)
            resource = (
                connection.execute(
                    select(PROJECT_RESOURCES).where(PROJECT_RESOURCES.c.resource_id == resource_id)
                )
                .mappings()
                .one_or_none()
            )
            if resource is None:
                return ResourceAccess(False, "resource_not_found")
            project_team = (
                connection.execute(
                    select(PROJECT_TEAMS.c.team_id).where(
                        and_(
                            PROJECT_TEAMS.c.project_id == resource["project_id"],
                            PROJECT_TEAMS.c.team_id == account["team_id"],
                        )
                    )
                ).scalar_one_or_none()
                is not None
            )
            shared = (
                connection.execute(
                    select(RESOURCE_SHARES.c.share_id).where(
                        and_(
                            RESOURCE_SHARES.c.resource_id == resource_id,
                            RESOURCE_SHARES.c.recipient_team_id == account["team_id"],
                        )
                    )
                ).scalar_one_or_none()
                is not None
            )
        propagation = DataPropagation(resource["propagation"])
        if account["team_id"] == resource["owner_team_id"]:
            if propagation is DataPropagation.TEAM_PRIVATE and action in {
                ResourceAction.SAVE,
                ResourceAction.RESHARE,
            }:
                return ResourceAccess(False, "team_private_no_propagation", resource["project_id"])
            return ResourceAccess(True, "owner_team", resource["project_id"])
        in_context = project_id == resource["project_id"]
        if propagation is DataPropagation.TEAM_PRIVATE:
            return ResourceAccess(False, "team_private")
        if propagation is DataPropagation.PROJECT_READONLY:
            allowed = (
                project_team
                and in_context
                and action in {ResourceAction.VIEW, ResourceAction.AGENT_USE}
            )
            return ResourceAccess(
                allowed,
                "project_readonly" if allowed else "project_readonly_no_export",
                resource["project_id"],
            )
        if project_team or shared:
            return ResourceAccess(
                True,
                "portable_project_team" if project_team else "explicit_team_share",
                resource["project_id"],
            )
        return ResourceAccess(False, "not_shared")


class ProjectResourceService:
    def __init__(self, engine: Engine, *, artifact_repository=None) -> None:
        self.engine = engine
        self.artifact_repository = artifact_repository

    def publishing_team(self, *, project_id: str, actor_id: str) -> str:
        """Authorize an upload before any immutable content is written."""
        with self.engine.connect() as connection:
            actor = ProductAccountService._account_row(connection, actor_id)
            participating = connection.execute(
                select(PROJECT_TEAMS.c.team_id).where(
                    and_(
                        PROJECT_TEAMS.c.project_id == project_id,
                        PROJECT_TEAMS.c.team_id == actor["team_id"],
                    )
                )
            ).scalar_one_or_none()
            if participating is None:
                raise PolicyDenied("only a participating team may publish project resources")
            return actor["team_id"]

    def publish(
        self,
        *,
        resource_id: str,
        project_id: str,
        actor_id: str,
        title: str,
        propagation: DataPropagation,
        artifact_id: str,
        artifact_sha256: str,
        allow_existing: bool = False,
    ) -> ProjectResource:
        ProductAccountService._identifier(resource_id)
        if not title.strip() or not re.fullmatch(r"[0-9a-f]{64}", artifact_sha256):
            raise ValueError("project resource metadata is invalid")
        now = datetime.now(UTC)
        team_id = self.publishing_team(project_id=project_id, actor_id=actor_id)
        if self.artifact_repository is None:
            raise ResourceNotFound("artifact repository is unavailable")
        from ..security.models import Classification, Principal

        manifest = self.artifact_repository.read(
            principal=Principal(
                actor_id, team_id, frozenset(), Classification.RESTRICTED, frozenset()
            ),
            owner_tenant_id=team_id,
            artifact_id=artifact_id,
            expected_sha256=artifact_sha256,
        )
        with self.engine.begin() as connection:
            existing = (
                connection.execute(
                    select(PROJECT_RESOURCES).where(PROJECT_RESOURCES.c.resource_id == resource_id)
                )
                .mappings()
                .one_or_none()
            )
            if existing is not None:
                expected = {
                    "project_id": project_id,
                    "owner_team_id": team_id,
                    "created_by": actor_id,
                    "title": title.strip(),
                    "artifact_owner_team_id": team_id,
                    "artifact_id": artifact_id,
                    "artifact_sha256": artifact_sha256,
                    "media_type": manifest.media_type,
                    "propagation": propagation.value,
                }
                if allow_existing and all(
                    existing[key] == value for key, value in expected.items()
                ):
                    return self._resource(existing)
                raise GovernanceConflictError("project resource identifier already exists")
            connection.execute(
                insert(PROJECT_RESOURCES).values(
                    resource_id=resource_id,
                    project_id=project_id,
                    owner_team_id=team_id,
                    created_by=actor_id,
                    title=title.strip(),
                    artifact_owner_team_id=team_id,
                    artifact_id=artifact_id,
                    artifact_sha256=artifact_sha256,
                    media_type=manifest.media_type,
                    propagation=propagation.value,
                    created_at=now,
                )
            )
            if propagation is not DataPropagation.TEAM_PRIVATE:
                connection.execute(
                    insert(PROJECT_ACTIVITIES).values(
                        project_id=project_id,
                        actor_account_id=actor_id,
                        actor_team_id=team_id,
                        event_type="resource.published",
                        subject_id=resource_id,
                        target_team_id=None,
                        summary=f"发布项目资料：{title.strip()}",
                        created_at=now,
                    )
                )
        return ProjectResource(
            resource_id,
            project_id,
            team_id,
            actor_id,
            title.strip(),
            team_id,
            artifact_id,
            artifact_sha256,
            manifest.media_type,
            propagation,
            now,
        )

    def list_visible(self, *, actor_id: str, project_id: str) -> list[ProjectResource]:
        with self.engine.connect() as connection:
            actor = ProductAccountService._account_row(connection, actor_id)
            participant = connection.execute(
                select(PROJECT_TEAMS.c.team_id).where(
                    and_(
                        PROJECT_TEAMS.c.project_id == project_id,
                        PROJECT_TEAMS.c.team_id == actor["team_id"],
                    )
                )
            ).scalar_one_or_none()
            if participant is None:
                raise ResourceNotFound("project is unavailable")
            rows = (
                connection.execute(
                    select(PROJECT_RESOURCES)
                    .where(
                        and_(
                            PROJECT_RESOURCES.c.project_id == project_id,
                            or_(
                                PROJECT_RESOURCES.c.owner_team_id == actor["team_id"],
                                PROJECT_RESOURCES.c.propagation
                                != DataPropagation.TEAM_PRIVATE.value,
                            ),
                        )
                    )
                    .order_by(PROJECT_RESOURCES.c.created_at.desc())
                )
                .mappings()
                .all()
            )
        return [self._resource(row) for row in rows]

    def get_authorized(
        self, *, actor_id: str, resource_id: str, action: ResourceAction, project_id: str | None
    ) -> ProjectResource:
        access = ProjectDataPolicy(self.engine).decide(
            account_id=actor_id, resource_id=resource_id, action=action, project_id=project_id
        )
        if not access.allowed:
            if access.reason == "resource_not_found":
                raise ResourceNotFound("project resource is unavailable")
            raise PolicyDenied(access.reason)
        with self.engine.connect() as connection:
            row = (
                connection.execute(
                    select(PROJECT_RESOURCES).where(PROJECT_RESOURCES.c.resource_id == resource_id)
                )
                .mappings()
                .one()
            )
        return self._resource(row)

    def save_to_library(
        self, *, actor_id: str, resource_id: str, project_id: str | None
    ) -> ProjectResource:
        resource = self.get_authorized(
            actor_id=actor_id,
            resource_id=resource_id,
            action=ResourceAction.SAVE,
            project_id=project_id,
        )
        with self.engine.begin() as connection:
            exists = connection.execute(
                select(PERSONAL_LIBRARY.c.resource_id).where(
                    and_(
                        PERSONAL_LIBRARY.c.account_id == actor_id,
                        PERSONAL_LIBRARY.c.resource_id == resource_id,
                    )
                )
            ).scalar_one_or_none()
            if exists is None:
                connection.execute(
                    insert(PERSONAL_LIBRARY).values(
                        account_id=actor_id, resource_id=resource_id, saved_at=datetime.now(UTC)
                    )
                )
        return resource

    def share_with_team(
        self,
        *,
        share_id: str,
        actor_id: str,
        resource_id: str,
        recipient_team_id: str,
        project_id: str | None,
    ) -> ProjectResource:
        resource = self.get_authorized(
            actor_id=actor_id,
            resource_id=resource_id,
            action=ResourceAction.RESHARE,
            project_id=project_id,
        )
        with self.engine.begin() as connection:
            actor = ProductAccountService._account_row(connection, actor_id)
            if not ProductAccountService._are_related(
                connection, actor["team_id"], recipient_team_id
            ):
                raise PolicyDenied("resources may only be shared with a related team")
            try:
                connection.execute(
                    insert(RESOURCE_SHARES).values(
                        share_id=share_id,
                        resource_id=resource_id,
                        shared_by=actor_id,
                        recipient_team_id=recipient_team_id,
                        created_at=datetime.now(UTC),
                    )
                )
            except Exception as exc:
                raise GovernanceConflictError("resource is already shared with this team") from exc
        return resource

    def agent_context_items(
        self, *, actor_id: str, project_id: str, resource_ids: tuple[str, ...], content_service
    ) -> tuple:
        if len(resource_ids) > 64 or len(set(resource_ids)) != len(resource_ids):
            raise ValueError("Agent project resource selection is invalid")
        from ..context import ContentTrust, ContextItem, ContextSource, InstructionTrust
        from ..security.models import Classification, DisclosureGrant, ResourceLabel

        items = []
        actor = self._account_for(actor_id)
        for resource_id in resource_ids:
            resource = self.get_authorized(
                actor_id=actor_id,
                resource_id=resource_id,
                action=ResourceAction.AGENT_USE,
                project_id=project_id,
            )
            if not (
                resource.media_type.startswith("text/")
                or resource.media_type
                in {
                    "application/json",
                    "application/xml",
                    "application/yaml",
                    "application/x-yaml",
                    "application/javascript",
                }
            ):
                raise PolicyDenied("selected Agent context resource is not text-readable")
            chunks = content_service.open_policy_authorized(
                owner_tenant_id=resource.artifact_owner_team_id, sha256=resource.artifact_sha256
            )
            raw = bytearray()
            for chunk in chunks:
                raw.extend(chunk)
                if len(raw) > 1_000_000:
                    raise PolicyDenied("selected Agent context resource exceeds 1 MB")
            try:
                content = raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise PolicyDenied("selected Agent context resource is not UTF-8 text") from exc
            label = ResourceLabel(
                resource.owner_team_id,
                Classification.INTERNAL,
                frozenset(),
                f"project-resource:{resource.resource_id}",
            )
            grant = None
            if actor.team_id != resource.owner_team_id:
                grant = DisclosureGrant(
                    f"grant-{resource.resource_id}-{actor.team_id}",
                    resource.owner_team_id,
                    actor.team_id,
                    label.resource_id,
                    f"project:{project_id}",
                    "project-data-policy",
                    datetime.now(UTC) + timedelta(hours=24),
                    Classification.INTERNAL,
                    frozenset(),
                )
            items.append(
                ContextItem(
                    item_id=resource.resource_id,
                    content=content,
                    source=ContextSource.DOCUMENT,
                    source_id=resource.artifact_id,
                    label=label,
                    content_trust=ContentTrust.VERIFIED,
                    instruction_trust=InstructionTrust.DATA_ONLY,
                    priority=100,
                    created_at=resource.created_at,
                    disclosure_grant=grant,
                )
            )
        return tuple(items)

    def _account_for(self, actor_id: str) -> Account:
        with self.engine.connect() as connection:
            return ProductAccountService._account(
                ProductAccountService._account_row(connection, actor_id)
            )

    @staticmethod
    def _resource(row) -> ProjectResource:
        return ProjectResource(
            row["resource_id"],
            row["project_id"],
            row["owner_team_id"],
            row["created_by"],
            row["title"],
            row["artifact_owner_team_id"],
            row["artifact_id"],
            row["artifact_sha256"],
            row["media_type"],
            DataPropagation(row["propagation"]),
            ProductAccountService._aware(row["created_at"]),
        )


class TeamCollaborationService:
    def __init__(self, engine: Engine, *, notifier=None) -> None:
        self.engine = engine
        self.notifier = notifier

    def send_message(
        self, *, message_id: str, project_id: str, actor_id: str, target_team_id: str, content: str
    ) -> ProjectMessage:
        ProductAccountService._identifier(message_id)
        if not content.strip() or len(content.encode("utf-8")) > 20_000:
            raise ValueError("project message content is invalid")
        now = datetime.now(UTC)
        with self.engine.begin() as connection:
            actor = self._participant(connection, project_id, actor_id)
            self._target(connection, project_id, target_team_id, actor["team_id"])
            connection.execute(
                insert(PROJECT_MESSAGES).values(
                    message_id=message_id,
                    project_id=project_id,
                    source_team_id=actor["team_id"],
                    target_team_id=target_team_id,
                    created_by=actor_id,
                    content=content.strip(),
                    created_at=now,
                )
            )
            self._activity(
                connection,
                project_id,
                actor,
                "message.sent",
                message_id,
                target_team_id,
                "团队发送了项目消息",
                now,
            )
        return ProjectMessage(
            message_id, project_id, actor["team_id"], target_team_id, actor_id, content.strip(), now
        )

    def list_messages(self, *, project_id: str, actor_id: str) -> list[ProjectMessage]:
        with self.engine.connect() as connection:
            actor = self._participant(connection, project_id, actor_id)
            rows = (
                connection.execute(
                    select(PROJECT_MESSAGES)
                    .where(
                        and_(
                            PROJECT_MESSAGES.c.project_id == project_id,
                            or_(
                                PROJECT_MESSAGES.c.source_team_id == actor["team_id"],
                                PROJECT_MESSAGES.c.target_team_id == actor["team_id"],
                            ),
                        )
                    )
                    .order_by(PROJECT_MESSAGES.c.created_at)
                )
                .mappings()
                .all()
            )
        return [
            ProjectMessage(
                row["message_id"],
                row["project_id"],
                row["source_team_id"],
                row["target_team_id"],
                row["created_by"],
                row["content"],
                ProductAccountService._aware(row["created_at"]),
            )
            for row in rows
        ]

    def create_task(
        self,
        *,
        task_id: str,
        project_id: str,
        actor_id: str,
        target_team_id: str,
        title: str,
        description: str,
        acceptance_criteria: str,
        priority: TaskPriority = TaskPriority.NORMAL,
        due_at: datetime | None = None,
    ) -> TeamTask:
        ProductAccountService._identifier(task_id)
        if not title.strip() or not acceptance_criteria.strip():
            raise ValueError("team task requires title and acceptance criteria")
        now = datetime.now(UTC)
        if due_at is not None:
            _validate_due_at(due_at, now)
        with self.engine.begin() as connection:
            actor = self._participant(connection, project_id, actor_id)
            self._target(connection, project_id, target_team_id, actor["team_id"])
            values = dict(
                task_id=task_id,
                project_id=project_id,
                source_team_id=actor["team_id"],
                target_team_id=target_team_id,
                created_by=actor_id,
                title=title.strip(),
                description=description.strip(),
                acceptance_criteria=acceptance_criteria.strip(),
                status=TeamTaskStatus.PROPOSED.value,
                assigned_account_id=None,
                artifact_resource_ids="[]",
                review_note="",
                priority=priority.value,
                due_at=due_at,
                schedule_version=1,
                due_changed_at=due_at,
                due_changed_by=actor_id,
                completed_at=None,
                created_at=now,
                updated_at=now,
            )
            connection.execute(insert(TEAM_TASKS).values(**values))
            self._activity(
                connection,
                project_id,
                actor,
                "task.proposed",
                task_id,
                target_team_id,
                f"向目标团队提出任务：{title.strip()}",
                now,
            )
            if priority is not TaskPriority.NORMAL or due_at is not None:
                self._activity(
                    connection,
                    project_id,
                    actor,
                    "task_schedule_set",
                    task_id,
                    target_team_id,
                    f"设置任务排期：优先级 {priority.value}"
                    + (f"，截止时间 {due_at.astimezone(UTC).strftime('%Y-%m-%d %H:%M UTC')}" if due_at else "，未设置截止时间"),
                    now,
                )
            if self.notifier is not None and due_at is not None:
                self.notifier.refresh_task_reminders(connection, values, now=now)
        return self._task(values)

    def respond_task(
        self, *, project_id: str, task_id: str, actor_id: str, accept: bool
    ) -> TeamTask:
        target = TeamTaskStatus.ACCEPTED if accept else TeamTaskStatus.REJECTED
        return self._transition(
            project_id,
            task_id,
            actor_id,
            expected={TeamTaskStatus.PROPOSED},
            target=target,
            side="target",
            event="task.accepted" if accept else "task.rejected",
            summary="目标团队接受了任务" if accept else "目标团队拒绝了任务",
        )

    def assign_internal(
        self, *, project_id: str, task_id: str, actor_id: str, account_id: str
    ) -> TeamTask:
        now = datetime.now(UTC)
        with self.engine.begin() as connection:
            actor = self._participant(connection, project_id, actor_id)
            task = self._task_row(connection, project_id, task_id)
            if task["target_team_id"] != actor["team_id"]:
                raise PolicyDenied("only the target team may assign internal responsibility")
            candidate = ProductAccountService._account_row(connection, account_id)
            if candidate["team_id"] != actor["team_id"]:
                raise PolicyDenied("internal responsibility must stay within the target team")
            connection.execute(
                update(TEAM_TASKS)
                .where(TEAM_TASKS.c.task_id == task_id)
                .values(assigned_account_id=account_id, updated_at=now)
            )
            task = dict(task)
            task["assigned_account_id"] = account_id
            task["updated_at"] = now
            self._activity(
                connection,
                project_id,
                actor,
                "task.assigned_internal",
                task_id,
                actor["team_id"],
                "目标团队已安排内部负责人",
                now,
            )
        return self._task(task)

    def start_task(self, *, project_id: str, task_id: str, actor_id: str) -> TeamTask:
        return self._transition(
            project_id,
            task_id,
            actor_id,
            expected={TeamTaskStatus.ACCEPTED, TeamTaskStatus.CHANGES_REQUESTED},
            target=TeamTaskStatus.IN_PROGRESS,
            side="target",
            event="task.started",
            summary="目标团队开始执行任务",
            require_assignment=True,
        )

    def submit_task(
        self, *, project_id: str, task_id: str, actor_id: str, resource_ids: tuple[str, ...]
    ) -> TeamTask:
        if (
            not resource_ids
            or len(resource_ids) > 32
            or len(set(resource_ids)) != len(resource_ids)
        ):
            raise ValueError("task submission requires unique project resources")
        now = datetime.now(UTC)
        with self.engine.begin() as connection:
            actor = self._participant(connection, project_id, actor_id)
            task = self._task_row(connection, project_id, task_id)
            if (
                task["target_team_id"] != actor["team_id"]
                or TeamTaskStatus(task["status"]) is not TeamTaskStatus.IN_PROGRESS
            ):
                raise PolicyDenied("target team task is not ready for submission")
            for resource_id in resource_ids:
                resource = connection.execute(
                    select(PROJECT_RESOURCES).where(
                        and_(
                            PROJECT_RESOURCES.c.project_id == project_id,
                            PROJECT_RESOURCES.c.resource_id == resource_id,
                            PROJECT_RESOURCES.c.owner_team_id == actor["team_id"],
                            PROJECT_RESOURCES.c.propagation != DataPropagation.TEAM_PRIVATE.value,
                        )
                    )
                ).scalar_one_or_none()
                if resource is None:
                    raise PolicyDenied(
                        "submitted resources must be project-visible and owned by target team"
                    )
            encoded = json.dumps(list(resource_ids), separators=(",", ":"))
            connection.execute(
                update(TEAM_TASKS)
                .where(TEAM_TASKS.c.task_id == task_id)
                .values(
                    status=TeamTaskStatus.SUBMITTED.value,
                    artifact_resource_ids=encoded,
                    updated_at=now,
                )
            )
            task = dict(task)
            task.update(
                status=TeamTaskStatus.SUBMITTED.value, artifact_resource_ids=encoded, updated_at=now
            )
            self._activity(
                connection,
                project_id,
                actor,
                "task.submitted",
                task_id,
                task["source_team_id"],
                "目标团队提交了任务交付",
                now,
            )
        return self._task(task)

    def review_task(
        self, *, project_id: str, task_id: str, actor_id: str, accept: bool, note: str
    ) -> TeamTask:
        target = TeamTaskStatus.VERIFIED if accept else TeamTaskStatus.CHANGES_REQUESTED
        return self._transition(
            project_id,
            task_id,
            actor_id,
            expected={TeamTaskStatus.SUBMITTED},
            target=target,
            side="source",
            event="task.verified" if accept else "task.changes_requested",
            summary="来源团队验收了任务" if accept else "来源团队要求修改任务",
            review_note=note.strip(),
        )

    def change_task_schedule(
        self,
        *,
        project_id: str,
        task_id: str,
        actor_id: str,
        priority: TaskPriority | None,
        due_at: datetime | None,
        clear_due_at: bool,
        expected_schedule_version: int,
        reason: str,
    ) -> tuple[str, TeamTask | TaskScheduleProposal]:
        """Change a task schedule directly or through a proposal.

        The source team may directly edit a task that has not been accepted yet.
        After acceptance either side negotiates through a proposal that the other
        team must decide. Shortening the deadline (or setting one where none
        existed) requires a reason. Optimistic schedule versions guard against
        concurrent edits and replayed requests.
        """
        if type(expected_schedule_version) is not int or expected_schedule_version < 1:
            raise ValueError("expected schedule version is invalid")
        if len(reason) > 2_000:
            raise ValueError("schedule change reason is too long")
        now = datetime.now(UTC)
        if due_at is not None:
            _validate_due_at(due_at, now)
        if clear_due_at and due_at is not None:
            raise ValueError("cannot clear and set a due time at the same time")
        with self.engine.begin() as connection:
            actor = self._participant(connection, project_id, actor_id)
            task = self._task_row(connection, project_id, task_id)
            team_id = actor["team_id"]
            if team_id not in (task["source_team_id"], task["target_team_id"]):
                raise PolicyDenied("only task parties may change the schedule")
            if task["schedule_version"] != expected_schedule_version:
                raise GovernanceConflictError("task schedule version is stale")
            status = TeamTaskStatus(task["status"])
            if status in TASK_TERMINAL_STATUSES:
                raise PolicyDenied("terminal tasks cannot change schedule")
            new_priority = priority if priority is not None else TaskPriority(task["priority"])
            old_due_at = (
                ProductAccountService._aware(task["due_at"])
                if task["due_at"] is not None
                else None
            )
            new_due_at = None if clear_due_at else (due_at if due_at is not None else old_due_at)
            shortened = (
                old_due_at is not None
                and new_due_at is not None
                and new_due_at < old_due_at
            )
            if shortened and not reason.strip():
                raise ValueError("shortening a task deadline requires a reason")
            unchanged = new_priority.value == task["priority"] and new_due_at == old_due_at
            direct_edit_allowed = (
                status is TeamTaskStatus.PROPOSED and team_id == task["source_team_id"]
            )
            if unchanged:
                if direct_edit_allowed:
                    return "updated", self._task(task)
                raise GovernanceConflictError("schedule change must alter the schedule")
            if direct_edit_allowed:
                updated = dict(task)
                updated.update(
                    priority=new_priority.value,
                    due_at=new_due_at,
                    schedule_version=task["schedule_version"] + 1,
                    due_changed_at=now if new_due_at != old_due_at else task["due_changed_at"],
                    due_changed_by=(
                        actor_id if new_due_at != old_due_at else task["due_changed_by"]
                    ),
                    updated_at=now,
                )
                connection.execute(
                    update(TEAM_TASKS)
                    .where(TEAM_TASKS.c.task_id == task_id)
                    .values(
                        priority=updated["priority"],
                        due_at=updated["due_at"],
                        schedule_version=updated["schedule_version"],
                        due_changed_at=updated["due_changed_at"],
                        due_changed_by=updated["due_changed_by"],
                        updated_at=now,
                    )
                )
                self._activity(
                    connection,
                    project_id,
                    actor,
                    "task_schedule_changed",
                    task_id,
                    task["target_team_id"],
                    self._schedule_change_summary(new_priority.value, old_due_at, new_due_at)
                    + (f"；原因：{reason.strip()}" if reason.strip() else ""),
                    now,
                )
                if self.notifier is not None:
                    self.notifier.refresh_task_reminders(connection, updated, now=now)
                return "updated", self._task(updated)
            existing_pending = (
                connection.execute(
                    select(TASK_SCHEDULE_PROPOSALS)
                    .where(
                        and_(
                            TASK_SCHEDULE_PROPOSALS.c.task_id == task_id,
                            TASK_SCHEDULE_PROPOSALS.c.status
                            == TaskScheduleProposalStatus.PENDING.value,
                        )
                    )
                    .with_for_update()
                )
                .mappings()
                .all()
            )
            def _stored_due(value):
                return (
                    ProductAccountService._aware(value) if value is not None else None
                )

            duplicate = next(
                (
                    row
                    for row in existing_pending
                    if row["proposed_by_team_id"] == team_id
                    and row["new_priority"] == new_priority.value
                    and _stored_due(row["new_due_at"]) == new_due_at
                ),
                None,
            )
            if duplicate is not None:
                return "proposed", self._proposal(duplicate)
            for row in existing_pending:
                connection.execute(
                    update(TASK_SCHEDULE_PROPOSALS)
                    .where(
                        TASK_SCHEDULE_PROPOSALS.c.proposal_id == row["proposal_id"]
                    )
                    .values(
                        status=TaskScheduleProposalStatus.SUPERSEDED.value,
                        version=row["version"] + 1,
                        decided_at=now,
                    )
                )
            proposal_id = f"proposal-{secrets.token_hex(12)}"
            connection.execute(
                insert(TASK_SCHEDULE_PROPOSALS).values(
                    proposal_id=proposal_id,
                    project_id=project_id,
                    task_id=task_id,
                    proposed_by=actor_id,
                    proposed_by_team_id=team_id,
                    decided_by_team_id=None,
                    old_priority=task["priority"],
                    new_priority=new_priority.value,
                    old_due_at=old_due_at,
                    new_due_at=new_due_at,
                    reason=reason.strip(),
                    decision_reason="",
                    status=TaskScheduleProposalStatus.PENDING.value,
                    version=1,
                    schedule_version=task["schedule_version"],
                    created_at=now,
                    decided_at=None,
                )
            )
            self._activity(
                connection,
                project_id,
                actor,
                "task_schedule_proposed",
                task_id,
                task["target_team_id"] if team_id == task["source_team_id"] else task["source_team_id"],
                f"提出任务排期变更提议：{self._schedule_change_summary(new_priority.value, old_due_at, new_due_at)}",
                now,
            )
            proposal_row = (
                connection.execute(
                    select(TASK_SCHEDULE_PROPOSALS).where(
                        TASK_SCHEDULE_PROPOSALS.c.proposal_id == proposal_id
                    )
                )
                .mappings()
                .one()
            )
            return "proposed", self._proposal(proposal_row)

    def decide_schedule_proposal(
        self,
        *,
        project_id: str,
        task_id: str,
        proposal_id: str,
        actor_id: str,
        accept: bool,
        reason: str,
        expected_proposal_version: int,
    ) -> TaskScheduleProposal:
        if type(expected_proposal_version) is not int or expected_proposal_version < 1:
            raise ValueError("expected proposal version is invalid")
        if len(reason) > 2_000:
            raise ValueError("proposal decision reason is too long")
        now = datetime.now(UTC)
        with self.engine.begin() as connection:
            actor = self._participant(connection, project_id, actor_id)
            task = self._task_row(connection, project_id, task_id)
            proposal = (
                connection.execute(
                    select(TASK_SCHEDULE_PROPOSALS)
                    .where(
                        and_(
                            TASK_SCHEDULE_PROPOSALS.c.project_id == project_id,
                            TASK_SCHEDULE_PROPOSALS.c.task_id == task_id,
                            TASK_SCHEDULE_PROPOSALS.c.proposal_id == proposal_id,
                        )
                    )
                    .with_for_update()
                )
                .mappings()
                .one_or_none()
            )
            if proposal is None:
                raise ResourceNotFound("schedule proposal is unavailable")
            if proposal["version"] != expected_proposal_version:
                raise GovernanceConflictError("schedule proposal version is stale")
            if TaskScheduleProposalStatus(proposal["status"]) is not TaskScheduleProposalStatus.PENDING:
                raise GovernanceConflictError("schedule proposal is no longer pending")
            team_id = actor["team_id"]
            other_team = (
                task["target_team_id"]
                if proposal["proposed_by_team_id"] == task["source_team_id"]
                else task["source_team_id"]
            )
            if team_id != other_team:
                raise PolicyDenied("only the other task party may decide the schedule proposal")
            if team_id == proposal["proposed_by_team_id"]:
                raise PolicyDenied("the proposing team cannot decide its own proposal")
            if TeamTaskStatus(task["status"]) in TASK_TERMINAL_STATUSES:
                raise PolicyDenied("terminal tasks cannot change schedule")
            if accept and proposal["schedule_version"] != task["schedule_version"]:
                raise GovernanceConflictError("task schedule changed since the proposal")
            target_status = (
                TaskScheduleProposalStatus.ACCEPTED
                if accept
                else TaskScheduleProposalStatus.REJECTED
            )
            connection.execute(
                update(TASK_SCHEDULE_PROPOSALS)
                .where(TASK_SCHEDULE_PROPOSALS.c.proposal_id == proposal_id)
                .values(
                    status=target_status.value,
                    version=proposal["version"] + 1,
                    decided_by_team_id=team_id,
                    decision_reason=reason.strip(),
                    decided_at=now,
                )
            )
            updated_proposal = dict(proposal)
            updated_proposal.update(
                status=target_status.value,
                version=proposal["version"] + 1,
                decided_by_team_id=team_id,
                decision_reason=reason.strip(),
                decided_at=now,
            )
            if accept:
                new_due_at = proposal["new_due_at"]
                old_due_at = (
                    ProductAccountService._aware(task["due_at"])
                    if task["due_at"] is not None
                    else None
                )
                aware_new_due = (
                    ProductAccountService._aware(new_due_at) if new_due_at is not None else None
                )
                deadline_moved = aware_new_due != old_due_at
                updated_task = dict(task)
                updated_task.update(
                    priority=proposal["new_priority"],
                    due_at=new_due_at,
                    schedule_version=task["schedule_version"] + 1,
                    due_changed_at=now if deadline_moved else task["due_changed_at"],
                    due_changed_by=actor_id if deadline_moved else task["due_changed_by"],
                    updated_at=now,
                )
                connection.execute(
                    update(TEAM_TASKS)
                    .where(TEAM_TASKS.c.task_id == task_id)
                    .values(
                        priority=updated_task["priority"],
                        due_at=updated_task["due_at"],
                        schedule_version=updated_task["schedule_version"],
                        due_changed_at=updated_task["due_changed_at"],
                        due_changed_by=updated_task["due_changed_by"],
                        updated_at=now,
                    )
                )
                self._activity(
                    connection,
                    project_id,
                    actor,
                    "task_schedule_changed",
                    task_id,
                    proposal["proposed_by_team_id"],
                    self._schedule_change_summary(
                        proposal["new_priority"], old_due_at, aware_new_due
                    )
                    + (f"；决定原因：{reason.strip()}" if reason.strip() else ""),
                    now,
                )
                if self.notifier is not None:
                    self.notifier.refresh_task_reminders(connection, updated_task, now=now)
            else:
                self._activity(
                    connection,
                    project_id,
                    actor,
                    "task_schedule_decided",
                    task_id,
                    proposal["proposed_by_team_id"],
                    "拒绝了任务排期变更提议"
                    + (f"；原因：{reason.strip()}" if reason.strip() else ""),
                    now,
                )
            return self._proposal(updated_proposal)

    def list_schedule_proposals(
        self, *, project_id: str, task_id: str, actor_id: str
    ) -> list[TaskScheduleProposal]:
        with self.engine.connect() as connection:
            actor = self._participant(connection, project_id, actor_id)
            task = self._task_row(connection, project_id, task_id)
            if actor["team_id"] not in (task["source_team_id"], task["target_team_id"]):
                raise PolicyDenied("only task parties may view schedule proposals")
            rows = (
                connection.execute(
                    select(TASK_SCHEDULE_PROPOSALS)
                    .where(
                        and_(
                            TASK_SCHEDULE_PROPOSALS.c.project_id == project_id,
                            TASK_SCHEDULE_PROPOSALS.c.task_id == task_id,
                        )
                    )
                    .order_by(TASK_SCHEDULE_PROPOSALS.c.created_at.desc())
                    .limit(100)
                )
                .mappings()
                .all()
            )
        return [self._proposal(row) for row in rows]

    @staticmethod
    def _schedule_change_summary(priority: str, old_due_at, new_due_at) -> str:
        def render(value):
            return (
                value.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC") if value is not None else "无截止时间"
            )

        return f"优先级 {priority}，截止时间 {render(old_due_at)} → {render(new_due_at)}"

    def list_tasks(self, *, project_id: str, actor_id: str) -> list[TeamTask]:
        with self.engine.connect() as connection:
            actor = self._participant(connection, project_id, actor_id)
            rows = (
                connection.execute(
                    select(TEAM_TASKS)
                    .where(
                        and_(
                            TEAM_TASKS.c.project_id == project_id,
                            or_(
                                TEAM_TASKS.c.source_team_id == actor["team_id"],
                                TEAM_TASKS.c.target_team_id == actor["team_id"],
                            ),
                        )
                    )
                    .order_by(TEAM_TASKS.c.created_at.desc())
                )
                .mappings()
                .all()
            )
        return [self._task(row) for row in rows]

    def list_activities(
        self, *, project_id: str, actor_id: str, after_sequence: int = 0
    ) -> list[ProjectActivity]:
        with self.engine.connect() as connection:
            self._participant(connection, project_id, actor_id)
            rows = (
                connection.execute(
                    select(PROJECT_ACTIVITIES)
                    .where(
                        and_(
                            PROJECT_ACTIVITIES.c.project_id == project_id,
                            PROJECT_ACTIVITIES.c.sequence > after_sequence,
                        )
                    )
                    .order_by(PROJECT_ACTIVITIES.c.sequence)
                    .limit(500)
                )
                .mappings()
                .all()
            )
        return [
            ProjectActivity(
                row["sequence"],
                row["project_id"],
                row["actor_account_id"],
                row["actor_team_id"],
                row["event_type"],
                row["subject_id"],
                row["target_team_id"],
                row["summary"],
                ProductAccountService._aware(row["created_at"]),
            )
            for row in rows
        ]

    def notification_summaries(self, *, actor_id: str) -> list[ProjectNotificationSummary]:
        with self.engine.connect() as connection:
            actor = ProductAccountService._account_row(connection, actor_id)
            projects = (
                connection.execute(
                    select(PROJECTS)
                    .join(PROJECT_TEAMS, PROJECT_TEAMS.c.project_id == PROJECTS.c.project_id)
                    .where(PROJECT_TEAMS.c.team_id == actor["team_id"])
                    .order_by(PROJECTS.c.created_at.desc())
                )
                .mappings()
                .all()
            )
            cursors = {
                row["project_id"]: row["last_read_sequence"]
                for row in connection.execute(
                    select(PROJECT_ACTIVITY_CURSORS).where(
                        PROJECT_ACTIVITY_CURSORS.c.account_id == actor_id
                    )
                )
                .mappings()
                .all()
            }
            result = []
            for project in projects:
                latest = (
                    connection.execute(
                        select(PROJECT_ACTIVITIES.c.sequence)
                        .where(PROJECT_ACTIVITIES.c.project_id == project["project_id"])
                        .order_by(PROJECT_ACTIVITIES.c.sequence.desc())
                        .limit(1)
                    ).scalar_one_or_none()
                    or 0
                )
                last_read = min(cursors.get(project["project_id"], 0), latest)
                unread = connection.execute(
                    select(PROJECT_ACTIVITIES.c.sequence).where(
                        and_(
                            PROJECT_ACTIVITIES.c.project_id == project["project_id"],
                            PROJECT_ACTIVITIES.c.sequence > last_read,
                            PROJECT_ACTIVITIES.c.actor_account_id != actor_id,
                        )
                    )
                ).all()
                result.append(
                    ProjectNotificationSummary(
                        project["project_id"], project["name"], len(unread), latest, last_read
                    )
                )
        return result

    def collaboration_inbox(
        self,
        *,
        actor_id: str,
        limit: int = 100,
        priority: TaskPriority | None = None,
        due_before: datetime | None = None,
        due_within_hours: int | None = None,
        overdue_only: bool = False,
        assigned_only: bool = False,
        project_id: str | None = None,
    ) -> CollaborationInbox:
        if type(limit) is not int or not 1 <= limit <= 500:
            raise ValueError("collaboration inbox limit is invalid")
        if due_within_hours is not None and (
            type(due_within_hours) is not int or not 1 <= due_within_hours <= 336
        ):
            raise ValueError("collaboration inbox due window is invalid")
        now = datetime.now(UTC)
        if due_before is not None and due_before.tzinfo is None:
            raise ValueError("due_before must carry an explicit timezone")
        with self.engine.connect() as connection:
            actor = ProductAccountService._account_row(connection, actor_id)
            team_id = actor["team_id"]
            task_filter = or_(
                and_(
                    TEAM_TASKS.c.target_team_id == team_id,
                    TEAM_TASKS.c.status.in_(
                        (
                            TeamTaskStatus.PROPOSED.value,
                            TeamTaskStatus.ACCEPTED.value,
                            TeamTaskStatus.CHANGES_REQUESTED.value,
                            TeamTaskStatus.IN_PROGRESS.value,
                        )
                    ),
                ),
                and_(
                    TEAM_TASKS.c.source_team_id == team_id,
                    TEAM_TASKS.c.status == TeamTaskStatus.SUBMITTED.value,
                ),
            )
            if project_id is not None:
                task_filter = and_(task_filter, TEAM_TASKS.c.project_id == project_id)
            if priority is not None:
                task_filter = and_(task_filter, TEAM_TASKS.c.priority == priority.value)
            if assigned_only:
                task_filter = and_(task_filter, TEAM_TASKS.c.assigned_account_id == actor_id)
            if due_before is not None:
                task_filter = and_(task_filter, TEAM_TASKS.c.due_at.isnot(None),
                                   TEAM_TASKS.c.due_at < due_before)
            action_count = connection.execute(
                select(func.count()).select_from(TEAM_TASKS).where(task_filter)
            ).scalar_one()
            task_rows = (
                connection.execute(
                    select(TEAM_TASKS, PROJECTS.c.name.label("project_name"))
                    .join(PROJECTS, PROJECTS.c.project_id == TEAM_TASKS.c.project_id)
                    .where(task_filter)
                    .limit(2000)
                )
                .mappings()
                .all()
            )
            cursor = PROJECT_ACTIVITY_CURSORS.alias("collaboration_inbox_cursor")
            activity_source = (
                PROJECT_ACTIVITIES.join(
                    PROJECTS,
                    PROJECTS.c.project_id == PROJECT_ACTIVITIES.c.project_id,
                )
                .join(
                    PROJECT_TEAMS,
                    and_(
                        PROJECT_TEAMS.c.project_id == PROJECT_ACTIVITIES.c.project_id,
                        PROJECT_TEAMS.c.team_id == team_id,
                    ),
                )
                .outerjoin(
                    cursor,
                    and_(
                        cursor.c.account_id == actor_id,
                        cursor.c.project_id == PROJECT_ACTIVITIES.c.project_id,
                    ),
                )
            )
            activity_filter = and_(
                PROJECT_ACTIVITIES.c.actor_account_id != actor_id,
                PROJECT_ACTIVITIES.c.sequence > func.coalesce(cursor.c.last_read_sequence, 0),
            )
            if project_id is not None:
                activity_filter = and_(
                    activity_filter, PROJECT_ACTIVITIES.c.project_id == project_id
                )
            unread_count = connection.execute(
                select(func.count()).select_from(activity_source).where(activity_filter)
            ).scalar_one()
            activity_rows = (
                connection.execute(
                    select(PROJECT_ACTIVITIES, PROJECTS.c.name.label("project_name"))
                    .select_from(activity_source)
                    .where(activity_filter)
                    .order_by(
                        PROJECT_ACTIVITIES.c.created_at.desc(),
                        PROJECT_ACTIVITIES.c.sequence.desc(),
                    )
                    .limit(limit)
                )
                .mappings()
                .all()
            )
        schedules = {
            row["task_id"]: compute_team_task_schedule(
                status=TeamTaskStatus(row["status"]),
                priority=TaskPriority(row["priority"]),
                due_at=ProductAccountService._aware(row["due_at"])
                if row["due_at"] is not None
                else None,
                schedule_version=row["schedule_version"],
                now=now,
                due_soon_hours=due_within_hours or 48,
            )
            for row in task_rows
        }
        if overdue_only:
            task_rows = [row for row in task_rows if schedules[row["task_id"]].is_overdue]
        elif due_within_hours is not None:
            task_rows = [
                row for row in task_rows if schedules[row["task_id"]].is_due_soon
            ]
        if overdue_only or due_within_hours is not None or assigned_only or (
            priority is not None or due_before is not None or project_id is not None
        ):
            action_count = len(task_rows)
        ordered = sorted(task_rows, key=lambda row: row["updated_at"], reverse=True)
        ordered = sorted(
            ordered,
            key=lambda row: schedules[row["task_id"]].due_at is None,
        )
        ordered = sorted(
            ordered,
            key=lambda row: schedules[row["task_id"]].due_at or now,
        )
        ordered = sorted(
            ordered,
            key=lambda row: TASK_PRIORITY_RANK[schedules[row["task_id"]].priority],
        )
        ordered = sorted(
            ordered, key=lambda row: not schedules[row["task_id"]].is_overdue
        )
        task_rows = ordered[:limit]
        actions = tuple(
            CollaborationInboxAction(
                row["project_name"], self._inbox_action(row, team_id), self._task(row)
            )
            for row in task_rows
        )
        unread = tuple(
            CollaborationInboxActivity(
                row["project_name"],
                ProjectActivity(
                    row["sequence"],
                    row["project_id"],
                    row["actor_account_id"],
                    row["actor_team_id"],
                    row["event_type"],
                    row["subject_id"],
                    row["target_team_id"],
                    row["summary"],
                    ProductAccountService._aware(row["created_at"]),
                ),
            )
            for row in activity_rows
        )
        return CollaborationInbox(actions, unread, int(action_count), int(unread_count))

    def agent_collaboration_inbox_brief(self, *, actor_id: str) -> str:
        inbox = self.collaboration_inbox(actor_id=actor_id, limit=50)
        with self.engine.connect() as connection:
            actor = ProductAccountService._account_row(connection, actor_id)
        now = datetime.now(UTC)
        action_entries = []
        for item in inbox.actions:
            schedule = compute_team_task_schedule(
                status=item.task.status,
                priority=item.task.priority,
                due_at=item.task.due_at,
                schedule_version=item.task.schedule_version,
                now=now,
            )
            action_entries.append(
                {
                    "project_id": item.task.project_id,
                    "project_name": item.project_name,
                    "required_action": item.action,
                    "task_id": item.task.task_id,
                    "title": item.task.title,
                    "description": item.task.description[:1_000],
                    "acceptance_criteria": item.task.acceptance_criteria[:1_000],
                    "status": item.task.status.value,
                    "priority": item.task.priority.value,
                    "due_at": item.task.due_at.isoformat() if item.task.due_at else None,
                    "is_overdue": schedule.is_overdue,
                    "is_due_soon": schedule.is_due_soon,
                    "due_in_seconds": schedule.due_in_seconds,
                    "schedule_version": item.task.schedule_version,
                    "source_team_id": item.task.source_team_id,
                    "target_team_id": item.task.target_team_id,
                    "assigned_to_current_account": item.task.assigned_account_id == actor_id,
                    "updated_at": item.task.updated_at.isoformat(),
                }
            )
        payload = {
            "schema": "coifesp.collaboration-inbox-brief.v1",
            "team_id": actor["team_id"],
            "generated_at": now.isoformat(),
            "action_count": inbox.action_count,
            "unread_count": inbox.unread_count,
            "actions_truncated": inbox.action_count > len(inbox.actions),
            "unread_activities_truncated": inbox.unread_count > len(inbox.unread_activities),
            "ordering_rule": (
                "服务端排序：逾期任务优先，其次 urgent>high>normal>low，再按截止时间升序（无截止时间靠后），"
                "最后按更新时间。priority/due_at/is_overdue 均为服务端计算的真实字段；"
                "回答排序原因时必须引用这些字段，不得编造或推测期限。"
            ),
            "actions": action_entries,
            "unread_activities": [
                {
                    "project_id": item.activity.project_id,
                    "project_name": item.project_name,
                    "sequence": item.activity.sequence,
                    "actor_team_id": item.activity.actor_team_id,
                    "event_type": item.activity.event_type,
                    "subject_id": item.activity.subject_id,
                    "target_team_id": item.activity.target_team_id,
                    "summary": item.activity.summary,
                    "created_at": item.activity.created_at.isoformat(),
                }
                for item in inbox.unread_activities
            ],
        }
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def bind_inbox_agent_run(self, *, run_id: str, actor_id: str, mode: str) -> InboxAgentRun:
        ProductAccountService._identifier(run_id)
        try:
            selected_mode = InboxAgentMode(mode)
        except ValueError as exc:
            raise ValueError("collaboration inbox Agent mode is invalid") from exc
        now = datetime.now(UTC)
        with self.engine.begin() as connection:
            actor = ProductAccountService._account_row(connection, actor_id)
            existing = (
                connection.execute(
                    select(INBOX_AGENT_RUNS).where(INBOX_AGENT_RUNS.c.run_id == run_id)
                )
                .mappings()
                .one_or_none()
            )
            if existing is not None:
                if existing["created_by"] != actor_id or existing["team_id"] != actor["team_id"]:
                    raise GovernanceConflictError(
                        "Agent run is already bound to another collaboration inbox"
                    )
                if existing["mode"] != selected_mode.value:
                    raise GovernanceConflictError("collaboration inbox Agent mode cannot change")
                return self._inbox_agent_run(existing)
            connection.execute(
                insert(INBOX_AGENT_RUNS).values(
                    run_id=run_id,
                    team_id=actor["team_id"],
                    created_by=actor_id,
                    mode=selected_mode.value,
                    created_at=now,
                )
            )
        return InboxAgentRun(run_id, actor["team_id"], actor_id, selected_mode, now)

    def list_inbox_agent_runs(self, *, actor_id: str, limit: int = 100) -> list[InboxAgentRun]:
        if type(limit) is not int or not 1 <= limit <= 500:
            raise ValueError("collaboration inbox Agent history limit is invalid")
        with self.engine.connect() as connection:
            ProductAccountService._account_row(connection, actor_id)
            rows = (
                connection.execute(
                    select(INBOX_AGENT_RUNS)
                    .where(INBOX_AGENT_RUNS.c.created_by == actor_id)
                    .order_by(INBOX_AGENT_RUNS.c.created_at.desc())
                    .limit(limit)
                )
                .mappings()
                .all()
            )
        return [self._inbox_agent_run(row) for row in rows]

    def mark_project_read(self, *, project_id: str, actor_id: str) -> ProjectNotificationSummary:
        now = datetime.now(UTC)
        with self.engine.begin() as connection:
            actor = self._participant(connection, project_id, actor_id)
            project = (
                connection.execute(select(PROJECTS).where(PROJECTS.c.project_id == project_id))
                .mappings()
                .one()
            )
            latest = (
                connection.execute(
                    select(PROJECT_ACTIVITIES.c.sequence)
                    .where(PROJECT_ACTIVITIES.c.project_id == project_id)
                    .order_by(PROJECT_ACTIVITIES.c.sequence.desc())
                    .limit(1)
                ).scalar_one_or_none()
                or 0
            )
            changed = connection.execute(
                update(PROJECT_ACTIVITY_CURSORS)
                .where(
                    and_(
                        PROJECT_ACTIVITY_CURSORS.c.account_id == actor_id,
                        PROJECT_ACTIVITY_CURSORS.c.project_id == project_id,
                    )
                )
                .values(last_read_sequence=latest, updated_at=now)
            )
            if changed.rowcount == 0:
                connection.execute(
                    insert(PROJECT_ACTIVITY_CURSORS).values(
                        account_id=actor_id,
                        project_id=project_id,
                        last_read_sequence=latest,
                        updated_at=now,
                    )
                )
        return ProjectNotificationSummary(project_id, project["name"], 0, latest, latest)

    def bind_project_agent_run(
        self,
        *,
        project_id: str,
        run_id: str,
        actor_id: str,
        mode: str = ProjectAgentMode.ANALYSIS.value,
    ) -> None:
        ProductAccountService._identifier(run_id)
        try:
            selected_mode = ProjectAgentMode(mode)
        except ValueError as exc:
            raise ValueError("project Agent mode is invalid") from exc
        with self.engine.begin() as connection:
            actor = self._participant(connection, project_id, actor_id)
            existing = (
                connection.execute(
                    select(PROJECT_AGENT_RUNS).where(PROJECT_AGENT_RUNS.c.run_id == run_id)
                )
                .mappings()
                .one_or_none()
            )
            if existing is not None:
                if existing["project_id"] != project_id or existing["created_by"] != actor_id:
                    raise GovernanceConflictError("Agent run is already bound to another project")
                if existing["mode"] != selected_mode.value:
                    raise GovernanceConflictError("Agent run mode cannot be changed")
                return
            connection.execute(
                insert(PROJECT_AGENT_RUNS).values(
                    project_id=project_id,
                    run_id=run_id,
                    team_id=actor["team_id"],
                    created_by=actor_id,
                    mode=selected_mode.value,
                    created_at=datetime.now(UTC),
                )
            )

    def assert_project_agent_run(
        self, *, project_id: str, run_id: str, actor_id: str, required_mode: str | None = None
    ) -> None:
        with self.engine.connect() as connection:
            actor = self._participant(connection, project_id, actor_id)
            row = (
                connection.execute(
                    select(PROJECT_AGENT_RUNS).where(
                        and_(
                            PROJECT_AGENT_RUNS.c.project_id == project_id,
                            PROJECT_AGENT_RUNS.c.run_id == run_id,
                            PROJECT_AGENT_RUNS.c.created_by == actor_id,
                            PROJECT_AGENT_RUNS.c.team_id == actor["team_id"],
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            raise PolicyDenied("Agent run is not bound to this project and account")
        if required_mode is not None and row["mode"] != ProjectAgentMode(required_mode).value:
            raise PolicyDenied("project Agent run mode does not permit this operation")

    def list_project_agent_runs(self, *, project_id: str, actor_id: str) -> list[ProjectAgentRun]:
        with self.engine.connect() as connection:
            actor = self._participant(connection, project_id, actor_id)
            rows = (
                connection.execute(
                    select(PROJECT_AGENT_RUNS)
                    .where(
                        and_(
                            PROJECT_AGENT_RUNS.c.project_id == project_id,
                            PROJECT_AGENT_RUNS.c.team_id == actor["team_id"],
                        )
                    )
                    .order_by(PROJECT_AGENT_RUNS.c.created_at.desc())
                )
                .mappings()
                .all()
            )
        return [
            ProjectAgentRun(
                row["project_id"],
                row["run_id"],
                row["team_id"],
                row["created_by"],
                ProjectAgentMode(row["mode"]),
                ProductAccountService._aware(row["created_at"]),
            )
            for row in rows
        ]

    def create_topic(
        self,
        *,
        topic_id: str,
        project_id: str,
        actor_id: str,
        title: str,
        context: str,
        source_agent_run_id: str | None = None,
    ) -> ProjectTopic:
        ProductAccountService._identifier(topic_id)
        if not title.strip() or not context.strip() or len(context.encode("utf-8")) > 50_000:
            raise ValueError("project topic content is invalid")
        if source_agent_run_id is not None:
            ProductAccountService._identifier(source_agent_run_id)
        now = datetime.now(UTC)
        with self.engine.begin() as connection:
            actor = self._participant(connection, project_id, actor_id)
            values = dict(
                topic_id=topic_id,
                project_id=project_id,
                proposed_by_team_id=actor["team_id"],
                created_by=actor_id,
                title=title.strip(),
                context=context.strip(),
                origin="agent_confirmed" if source_agent_run_id else "human",
                source_agent_run_id=source_agent_run_id,
                status=ProjectTopicStatus.OPEN.value,
                decision="",
                decided_by_team_id=None,
                created_at=now,
                updated_at=now,
            )
            connection.execute(insert(PROJECT_TOPICS).values(**values))
            self._activity(
                connection,
                project_id,
                actor,
                "topic.opened",
                topic_id,
                None,
                f"团队提出项目议题：{title.strip()}",
                now,
            )
        return self._topic(values)

    def list_topics(self, *, project_id: str, actor_id: str) -> list[ProjectTopic]:
        with self.engine.connect() as connection:
            self._participant(connection, project_id, actor_id)
            rows = (
                connection.execute(
                    select(PROJECT_TOPICS)
                    .where(PROJECT_TOPICS.c.project_id == project_id)
                    .order_by(PROJECT_TOPICS.c.created_at.desc())
                )
                .mappings()
                .all()
            )
        return [self._topic(row) for row in rows]

    def contribute_topic(
        self, *, contribution_id: str, project_id: str, topic_id: str, actor_id: str, content: str
    ) -> ProjectTopicContribution:
        ProductAccountService._identifier(contribution_id)
        if not content.strip() or len(content.encode("utf-8")) > 20_000:
            raise ValueError("topic contribution content is invalid")
        now = datetime.now(UTC)
        with self.engine.begin() as connection:
            actor = self._participant(connection, project_id, actor_id)
            topic = self._topic_row(connection, project_id, topic_id)
            if topic["status"] != ProjectTopicStatus.OPEN.value:
                raise GovernanceConflictError("project topic is closed")
            values = dict(
                contribution_id=contribution_id,
                topic_id=topic_id,
                project_id=project_id,
                team_id=actor["team_id"],
                created_by=actor_id,
                content=content.strip(),
                created_at=now,
            )
            connection.execute(insert(PROJECT_TOPIC_CONTRIBUTIONS).values(**values))
            self._activity(
                connection,
                project_id,
                actor,
                "topic.contributed",
                topic_id,
                None,
                "团队参与了项目议题讨论",
                now,
            )
        return self._contribution(values)

    def list_topic_contributions(
        self, *, project_id: str, topic_id: str, actor_id: str
    ) -> list[ProjectTopicContribution]:
        with self.engine.connect() as connection:
            self._participant(connection, project_id, actor_id)
            self._topic_row(connection, project_id, topic_id, for_update=False)
            rows = (
                connection.execute(
                    select(PROJECT_TOPIC_CONTRIBUTIONS)
                    .where(
                        and_(
                            PROJECT_TOPIC_CONTRIBUTIONS.c.project_id == project_id,
                            PROJECT_TOPIC_CONTRIBUTIONS.c.topic_id == topic_id,
                        )
                    )
                    .order_by(PROJECT_TOPIC_CONTRIBUTIONS.c.created_at)
                )
                .mappings()
                .all()
            )
        return [self._contribution(row) for row in rows]

    def decide_topic(
        self, *, project_id: str, topic_id: str, actor_id: str, decision: str
    ) -> ProjectTopic:
        if not decision.strip() or len(decision.encode("utf-8")) > 50_000:
            raise ValueError("topic decision is invalid")
        now = datetime.now(UTC)
        with self.engine.begin() as connection:
            actor = self._participant(connection, project_id, actor_id)
            project = (
                connection.execute(select(PROJECTS).where(PROJECTS.c.project_id == project_id))
                .mappings()
                .one()
            )
            if actor["team_id"] != project["owner_team_id"]:
                raise PolicyDenied("only the initiating team may confirm a project decision")
            topic = self._topic_row(connection, project_id, topic_id)
            if topic["status"] != ProjectTopicStatus.OPEN.value:
                raise GovernanceConflictError("project topic is closed")
            values = {
                "status": ProjectTopicStatus.DECIDED.value,
                "decision": decision.strip(),
                "decided_by_team_id": actor["team_id"],
                "updated_at": now,
            }
            connection.execute(
                update(PROJECT_TOPICS).where(PROJECT_TOPICS.c.topic_id == topic_id).values(**values)
            )
            topic = dict(topic)
            topic.update(values)
            self._activity(
                connection,
                project_id,
                actor,
                "topic.decided",
                topic_id,
                None,
                f"发起团队确认项目决议：{topic['title']}",
                now,
            )
        return self._topic(topic)

    def import_action_drafts(
        self, *, project_id: str, actor_id: str, run_id: str, message_sequence: int, content: str
    ) -> list[CollaborationActionDraft]:
        self.assert_project_agent_run(
            project_id=project_id,
            run_id=run_id,
            actor_id=actor_id,
            required_mode=ProjectAgentMode.COLLABORATION_ACTIONS.value,
        )
        if message_sequence < 1 or len(content.encode("utf-8")) > 200_000:
            raise ValueError("Agent action message is invalid")
        try:
            document = json.loads(content, object_pairs_hook=self._unique_json_object)
        except (json.JSONDecodeError, ValueError) as exc:
            raise ValueError("Agent collaboration actions must be strict JSON") from exc
        if (
            not isinstance(document, dict)
            or set(document) != {"schema", "actions"}
            or document["schema"] != "coifesp.collaboration-actions.v1"
            or not isinstance(document["actions"], list)
            or not 1 <= len(document["actions"]) <= 20
        ):
            raise ValueError("Agent collaboration action document is invalid")
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        now = datetime.now(UTC)
        results = []
        with self.engine.begin() as connection:
            actor = self._participant(connection, project_id, actor_id)
            for index, raw in enumerate(document["actions"]):
                kind, payload = self._draft_payload(connection, project_id, actor["team_id"], raw)
                draft_id = f"draft-{hashlib.sha256(f'{project_id}:{run_id}:{message_sequence}:{index}'.encode()).hexdigest()[:24]}"
                existing = (
                    connection.execute(
                        select(COLLABORATION_ACTION_DRAFTS).where(
                            and_(
                                COLLABORATION_ACTION_DRAFTS.c.project_id == project_id,
                                COLLABORATION_ACTION_DRAFTS.c.source_agent_run_id == run_id,
                                COLLABORATION_ACTION_DRAFTS.c.source_message_sequence
                                == message_sequence,
                                COLLABORATION_ACTION_DRAFTS.c.action_index == index,
                            )
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if existing is None:
                    values = dict(
                        draft_id=draft_id,
                        project_id=project_id,
                        source_agent_run_id=run_id,
                        source_message_sequence=message_sequence,
                        source_content_sha256=digest,
                        action_index=index,
                        kind=kind.value,
                        payload=json.dumps(
                            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                        ),
                        status=CollaborationDraftStatus.PENDING.value,
                        version=1,
                        created_by=actor_id,
                        team_id=actor["team_id"],
                        executed_subject_id=None,
                        rejection_reason="",
                        created_at=now,
                        updated_at=now,
                    )
                    connection.execute(insert(COLLABORATION_ACTION_DRAFTS).values(**values))
                    self._activity(
                        connection,
                        project_id,
                        actor,
                        "agent_draft.imported",
                        draft_id,
                        payload.get("target_team_id"),
                        f"Agent 协作草案待确认：{kind.value}",
                        now,
                    )
                    existing = values
                elif existing["source_content_sha256"] != digest:
                    raise GovernanceConflictError(
                        "Agent message content changed after draft import"
                    )
                results.append(self._draft(existing))
        return results

    def list_action_drafts(
        self, *, project_id: str, actor_id: str
    ) -> list[CollaborationActionDraft]:
        with self.engine.connect() as connection:
            actor = self._participant(connection, project_id, actor_id)
            rows = (
                connection.execute(
                    select(COLLABORATION_ACTION_DRAFTS)
                    .where(
                        and_(
                            COLLABORATION_ACTION_DRAFTS.c.project_id == project_id,
                            COLLABORATION_ACTION_DRAFTS.c.team_id == actor["team_id"],
                        )
                    )
                    .order_by(
                        COLLABORATION_ACTION_DRAFTS.c.created_at.desc(),
                        COLLABORATION_ACTION_DRAFTS.c.action_index,
                    )
                )
                .mappings()
                .all()
            )
        return [self._draft(row) for row in rows]

    def update_action_draft(
        self, *, project_id: str, draft_id: str, actor_id: str, expected_version: int, payload: dict
    ) -> CollaborationActionDraft:
        now = datetime.now(UTC)
        with self.engine.begin() as connection:
            actor = self._participant(connection, project_id, actor_id)
            row = self._draft_row(connection, project_id, draft_id, actor["team_id"])
            if (
                row["status"] != CollaborationDraftStatus.PENDING.value
                or row["version"] != expected_version
            ):
                raise GovernanceConflictError("collaboration draft version or status changed")
            kind, validated = self._draft_payload(
                connection, project_id, actor["team_id"], {"kind": row["kind"], "payload": payload}
            )
            version = expected_version + 1
            values = {
                "payload": json.dumps(
                    validated, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                ),
                "version": version,
                "updated_at": now,
            }
            connection.execute(
                update(COLLABORATION_ACTION_DRAFTS)
                .where(COLLABORATION_ACTION_DRAFTS.c.draft_id == draft_id)
                .values(**values)
            )
            row = dict(row)
            row.update(values)
            row["kind"] = kind.value
        return self._draft(row)

    def reject_action_draft(
        self, *, project_id: str, draft_id: str, actor_id: str, expected_version: int, reason: str
    ) -> CollaborationActionDraft:
        if not reason.strip() or len(reason.encode("utf-8")) > 2_000:
            raise ValueError("draft rejection reason is invalid")
        return self._finish_draft(
            project_id=project_id,
            draft_id=draft_id,
            actor_id=actor_id,
            expected_version=expected_version,
            execute=False,
            rejection_reason=reason.strip(),
        )

    def execute_action_draft(
        self, *, project_id: str, draft_id: str, actor_id: str, expected_version: int
    ) -> CollaborationActionDraft:
        return self._finish_draft(
            project_id=project_id,
            draft_id=draft_id,
            actor_id=actor_id,
            expected_version=expected_version,
            execute=True,
            rejection_reason="",
        )

    def _finish_draft(
        self, *, project_id, draft_id, actor_id, expected_version, execute, rejection_reason
    ):
        now = datetime.now(UTC)
        with self.engine.begin() as connection:
            actor = self._participant(connection, project_id, actor_id)
            row = self._draft_row(connection, project_id, draft_id, actor["team_id"])
            if (
                row["status"] != CollaborationDraftStatus.PENDING.value
                or row["version"] != expected_version
            ):
                raise GovernanceConflictError("collaboration draft version or status changed")
            payload = json.loads(row["payload"])
            subject_id = None
            if execute:
                subject_id = f"{row['kind']}-{draft_id}"
                self._execute_draft(connection, actor, row, payload, subject_id, now)
            values = {
                "status": (
                    CollaborationDraftStatus.EXECUTED.value
                    if execute
                    else CollaborationDraftStatus.REJECTED.value
                ),
                "executed_subject_id": subject_id,
                "rejection_reason": rejection_reason,
                "version": expected_version + 1,
                "updated_at": now,
            }
            connection.execute(
                update(COLLABORATION_ACTION_DRAFTS)
                .where(COLLABORATION_ACTION_DRAFTS.c.draft_id == draft_id)
                .values(**values)
            )
            row = dict(row)
            row.update(values)
            self._activity(
                connection,
                project_id,
                actor,
                "agent_draft.executed" if execute else "agent_draft.rejected",
                draft_id,
                payload.get("target_team_id"),
                "Agent 协作草案已执行" if execute else "Agent 协作草案已拒绝",
                now,
            )
        return self._draft(row)

    def _execute_draft(self, connection, actor, row, payload, subject_id, now):
        project_id = row["project_id"]
        kind = CollaborationDraftKind(row["kind"])
        if kind is CollaborationDraftKind.MESSAGE:
            connection.execute(
                insert(PROJECT_MESSAGES).values(
                    message_id=subject_id,
                    project_id=project_id,
                    source_team_id=actor["team_id"],
                    target_team_id=payload["target_team_id"],
                    created_by=actor["account_id"],
                    content=payload["content"],
                    created_at=now,
                )
            )
            self._activity(
                connection,
                project_id,
                actor,
                "message.sent",
                subject_id,
                payload["target_team_id"],
                "团队确认并发送了 Agent 起草的项目消息",
                now,
            )
        elif kind is CollaborationDraftKind.TASK:
            connection.execute(
                insert(TEAM_TASKS).values(
                    task_id=subject_id,
                    project_id=project_id,
                    source_team_id=actor["team_id"],
                    target_team_id=payload["target_team_id"],
                    created_by=actor["account_id"],
                    title=payload["title"],
                    description=payload["description"],
                    acceptance_criteria=payload["acceptance_criteria"],
                    status=TeamTaskStatus.PROPOSED.value,
                    assigned_account_id=None,
                    artifact_resource_ids="[]",
                    review_note="",
                    created_at=now,
                    updated_at=now,
                )
            )
            self._activity(
                connection,
                project_id,
                actor,
                "task.proposed",
                subject_id,
                payload["target_team_id"],
                f"团队确认并提出 Agent 起草的任务：{payload['title']}",
                now,
            )
        else:
            connection.execute(
                insert(PROJECT_TOPICS).values(
                    topic_id=subject_id,
                    project_id=project_id,
                    proposed_by_team_id=actor["team_id"],
                    created_by=actor["account_id"],
                    title=payload["title"],
                    context=payload["context"],
                    origin="agent_confirmed",
                    source_agent_run_id=row["source_agent_run_id"],
                    status=ProjectTopicStatus.OPEN.value,
                    decision="",
                    decided_by_team_id=None,
                    created_at=now,
                    updated_at=now,
                )
            )
            self._activity(
                connection,
                project_id,
                actor,
                "topic.opened",
                subject_id,
                None,
                f"团队确认并提出 Agent 起草的议题：{payload['title']}",
                now,
            )

    def _draft_payload(self, connection, project_id, source_team_id, raw):
        if (
            not isinstance(raw, dict)
            or set(raw) != {"kind", "payload"}
            or not isinstance(raw["payload"], dict)
        ):
            raise ValueError("Agent collaboration action shape is invalid")
        try:
            kind = CollaborationDraftKind(raw["kind"])
        except (TypeError, ValueError) as exc:
            raise ValueError("Agent collaboration action kind is invalid") from exc
        payload = raw["payload"]
        fields = (
            {"target_team_id", "content"}
            if kind is CollaborationDraftKind.MESSAGE
            else (
                {"target_team_id", "title", "description", "acceptance_criteria"}
                if kind is CollaborationDraftKind.TASK
                else {"title", "context"}
            )
        )
        if set(payload) != fields or any(not isinstance(payload[name], str) for name in fields):
            raise ValueError("Agent collaboration action payload is invalid")
        payload = {name: payload[name].strip() for name in fields}
        if any(not value for value in payload.values()):
            raise ValueError("Agent collaboration action values are required")
        if kind in {CollaborationDraftKind.MESSAGE, CollaborationDraftKind.TASK}:
            self._target(connection, project_id, payload["target_team_id"], source_team_id)
        if kind is CollaborationDraftKind.MESSAGE and len(payload["content"].encode()) > 20_000:
            raise ValueError("Agent message draft is too large")
        if kind is CollaborationDraftKind.TASK and (
            len(payload["title"]) > 256
            or len(payload["description"].encode()) > 20_000
            or len(payload["acceptance_criteria"].encode()) > 20_000
        ):
            raise ValueError("Agent task draft is too large")
        if kind is CollaborationDraftKind.TOPIC and (
            len(payload["title"]) > 256 or len(payload["context"].encode()) > 50_000
        ):
            raise ValueError("Agent topic draft is too large")
        return kind, payload

    @staticmethod
    def _unique_json_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    @staticmethod
    def _draft_row(connection, project_id, draft_id, team_id):
        row = (
            connection.execute(
                select(COLLABORATION_ACTION_DRAFTS)
                .where(
                    and_(
                        COLLABORATION_ACTION_DRAFTS.c.project_id == project_id,
                        COLLABORATION_ACTION_DRAFTS.c.draft_id == draft_id,
                        COLLABORATION_ACTION_DRAFTS.c.team_id == team_id,
                    )
                )
                .with_for_update()
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise ResourceNotFound("collaboration draft is unavailable")
        return row

    def agent_project_brief(self, *, project_id: str, actor_id: str) -> str:
        with self.engine.connect() as connection:
            self._participant(connection, project_id, actor_id)
            project = (
                connection.execute(select(PROJECTS).where(PROJECTS.c.project_id == project_id))
                .mappings()
                .one()
            )
            teams = (
                connection.execute(
                    select(PROJECT_TEAMS)
                    .where(PROJECT_TEAMS.c.project_id == project_id)
                    .order_by(PROJECT_TEAMS.c.name)
                )
                .mappings()
                .all()
            )
        tasks = self.list_tasks(project_id=project_id, actor_id=actor_id)
        messages = self.list_messages(project_id=project_id, actor_id=actor_id)[-20:]
        activities = self.list_activities(project_id=project_id, actor_id=actor_id)[-50:]
        topics = self.list_topics(project_id=project_id, actor_id=actor_id)
        topic_contributions = {
            item.topic_id: self.list_topic_contributions(
                project_id=project_id, topic_id=item.topic_id, actor_id=actor_id
            )
            for item in topics
        }
        payload = {
            "schema": "coifesp.team-project-brief.v1",
            "project": {
                "project_id": project["project_id"],
                "name": project["name"],
                "description": project["description"],
                "owner_team_id": project["owner_team_id"],
            },
            "teams": [
                {"team_id": row["team_id"], "assignment": row["name"], "kind": row["kind"]}
                for row in teams
            ],
            "tasks": [
                {
                    "task_id": item.task_id,
                    "title": item.title,
                    "source_team_id": item.source_team_id,
                    "target_team_id": item.target_team_id,
                    "status": item.status.value,
                    "acceptance_criteria": item.acceptance_criteria,
                    "assigned": item.assigned_account_id is not None,
                }
                for item in tasks
            ],
            "recent_messages": [
                {
                    "source_team_id": item.source_team_id,
                    "target_team_id": item.target_team_id,
                    "content": item.content,
                }
                for item in messages
            ],
            "recent_activity": [
                {
                    "event_type": item.event_type,
                    "actor_team_id": item.actor_team_id,
                    "target_team_id": item.target_team_id,
                    "summary": item.summary,
                }
                for item in activities
            ],
            "topics": [
                {
                    "topic_id": item.topic_id,
                    "title": item.title,
                    "context": item.context,
                    "status": item.status.value,
                    "decision": item.decision,
                    "proposed_by_team_id": item.proposed_by_team_id,
                    "contributions": [
                        {"team_id": contribution.team_id, "content": contribution.content}
                        for contribution in topic_contributions[item.topic_id]
                    ],
                }
                for item in topics
            ],
        }
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def _transition(
        self,
        project_id,
        task_id,
        actor_id,
        *,
        expected,
        target,
        side,
        event,
        summary,
        require_assignment=False,
        review_note=None,
    ):
        now = datetime.now(UTC)
        with self.engine.begin() as connection:
            actor = self._participant(connection, project_id, actor_id)
            task = self._task_row(connection, project_id, task_id)
            expected_team = task[f"{side}_team_id"]
            if expected_team != actor["team_id"] or TeamTaskStatus(task["status"]) not in expected:
                raise PolicyDenied("team task transition is not authorized")
            if require_assignment and not task["assigned_account_id"]:
                raise GovernanceConflictError("target team must assign an internal owner first")
            values = {"status": target.value, "updated_at": now}
            if review_note is not None:
                values["review_note"] = review_note
            if target is TeamTaskStatus.VERIFIED:
                values["completed_at"] = now
            connection.execute(
                update(TEAM_TASKS).where(TEAM_TASKS.c.task_id == task_id).values(**values)
            )
            task = dict(task)
            task.update(values)
            self._activity(
                connection,
                project_id,
                actor,
                event,
                task_id,
                task["target_team_id"] if side == "source" else task["source_team_id"],
                summary,
                now,
            )
            if (
                self.notifier is not None
                and target is TeamTaskStatus.VERIFIED
                and task["due_at"] is not None
            ):
                self.notifier.refresh_task_reminders(connection, task, now=now)
        return self._task(task)

    @staticmethod
    def _participant(connection, project_id, actor_id):
        actor = ProductAccountService._account_row(connection, actor_id)
        exists = connection.execute(
            select(PROJECT_TEAMS.c.team_id).where(
                and_(
                    PROJECT_TEAMS.c.project_id == project_id,
                    PROJECT_TEAMS.c.team_id == actor["team_id"],
                )
            )
        ).scalar_one_or_none()
        if exists is None:
            raise ResourceNotFound("project is unavailable")
        return actor

    @staticmethod
    def _target(connection, project_id, target_team_id, source_team_id):
        if target_team_id == source_team_id:
            raise ValueError("cross-team action requires another team")
        exists = connection.execute(
            select(PROJECT_TEAMS.c.team_id).where(
                and_(
                    PROJECT_TEAMS.c.project_id == project_id,
                    PROJECT_TEAMS.c.team_id == target_team_id,
                )
            )
        ).scalar_one_or_none()
        if exists is None:
            raise ResourceNotFound("target project team is unavailable")

    @staticmethod
    def _task_row(connection, project_id, task_id):
        row = (
            connection.execute(
                select(TEAM_TASKS)
                .where(and_(TEAM_TASKS.c.project_id == project_id, TEAM_TASKS.c.task_id == task_id))
                .with_for_update()
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise ResourceNotFound("team task is unavailable")
        return row

    @staticmethod
    def _topic_row(connection, project_id, topic_id, for_update=True):
        query = select(PROJECT_TOPICS).where(
            and_(PROJECT_TOPICS.c.project_id == project_id, PROJECT_TOPICS.c.topic_id == topic_id)
        )
        if for_update:
            query = query.with_for_update()
        row = connection.execute(query).mappings().one_or_none()
        if row is None:
            raise ResourceNotFound("project topic is unavailable")
        return row

    def _activity(
        self, connection, project_id, actor, event_type, subject_id, target_team_id, summary, created_at
    ):
        result = connection.execute(
            insert(PROJECT_ACTIVITIES).values(
                project_id=project_id,
                actor_account_id=actor["account_id"],
                actor_team_id=actor["team_id"],
                event_type=event_type,
                subject_id=subject_id,
                target_team_id=target_team_id,
                summary=summary[:512],
                created_at=created_at,
            )
        )
        if self.notifier is not None:
            self.notifier.project_activity(
                connection,
                project_id=project_id,
                actor_account_id=actor["account_id"],
                sequence=int(result.inserted_primary_key[0]),
                event_type=event_type,
                subject_id=subject_id,
                summary=summary[:512],
                created_at=created_at,
                title=summary[:64],
            )

    @staticmethod
    def _inbox_action(row, team_id: str) -> str:
        status = TeamTaskStatus(row["status"])
        if row["source_team_id"] == team_id:
            return "review"
        if status is TeamTaskStatus.PROPOSED:
            return "respond"
        if status in {TeamTaskStatus.ACCEPTED, TeamTaskStatus.CHANGES_REQUESTED}:
            return "start" if row["assigned_account_id"] else "assign"
        if status is TeamTaskStatus.IN_PROGRESS:
            return "submit"
        raise RuntimeError("non-actionable team task entered the collaboration inbox")

    @staticmethod
    def _inbox_agent_run(row) -> InboxAgentRun:
        return InboxAgentRun(
            row["run_id"],
            row["team_id"],
            row["created_by"],
            InboxAgentMode(row["mode"]),
            ProductAccountService._aware(row["created_at"]),
        )

    @staticmethod
    def _task(row) -> TeamTask:
        return TeamTask(
            row["task_id"],
            row["project_id"],
            row["source_team_id"],
            row["target_team_id"],
            row["created_by"],
            row["title"],
            row["description"],
            row["acceptance_criteria"],
            TeamTaskStatus(row["status"]),
            row["assigned_account_id"],
            tuple(json.loads(row["artifact_resource_ids"])),
            row["review_note"],
            ProductAccountService._aware(row["created_at"]),
            ProductAccountService._aware(row["updated_at"]),
            TaskPriority(row["priority"]),
            ProductAccountService._aware(row["due_at"]) if row["due_at"] is not None else None,
            row["schedule_version"],
            ProductAccountService._aware(row["due_changed_at"])
            if row["due_changed_at"] is not None
            else None,
            row["due_changed_by"],
            ProductAccountService._aware(row["completed_at"])
            if row["completed_at"] is not None
            else None,
        )

    @staticmethod
    def _proposal(row) -> TaskScheduleProposal:
        return TaskScheduleProposal(
            row["proposal_id"],
            row["project_id"],
            row["task_id"],
            row["proposed_by"],
            row["proposed_by_team_id"],
            row["decided_by_team_id"],
            TaskPriority(row["old_priority"]),
            TaskPriority(row["new_priority"]),
            ProductAccountService._aware(row["old_due_at"])
            if row["old_due_at"] is not None
            else None,
            ProductAccountService._aware(row["new_due_at"])
            if row["new_due_at"] is not None
            else None,
            row["reason"],
            row["decision_reason"],
            TaskScheduleProposalStatus(row["status"]),
            row["version"],
            row["schedule_version"],
            ProductAccountService._aware(row["created_at"]),
            ProductAccountService._aware(row["decided_at"])
            if row["decided_at"] is not None
            else None,
        )

    @staticmethod
    def _topic(row) -> ProjectTopic:
        return ProjectTopic(
            row["topic_id"],
            row["project_id"],
            row["proposed_by_team_id"],
            row["created_by"],
            row["title"],
            row["context"],
            row["origin"],
            row["source_agent_run_id"],
            ProjectTopicStatus(row["status"]),
            row["decision"],
            row["decided_by_team_id"],
            ProductAccountService._aware(row["created_at"]),
            ProductAccountService._aware(row["updated_at"]),
        )

    @staticmethod
    def _contribution(row) -> ProjectTopicContribution:
        return ProjectTopicContribution(
            row["contribution_id"],
            row["topic_id"],
            row["project_id"],
            row["team_id"],
            row["created_by"],
            row["content"],
            ProductAccountService._aware(row["created_at"]),
        )

    @staticmethod
    def _draft(row) -> CollaborationActionDraft:
        return CollaborationActionDraft(
            row["draft_id"],
            row["project_id"],
            row["source_agent_run_id"],
            row["source_message_sequence"],
            row["source_content_sha256"],
            row["action_index"],
            CollaborationDraftKind(row["kind"]),
            json.loads(row["payload"]),
            CollaborationDraftStatus(row["status"]),
            row["version"],
            row["created_by"],
            row["team_id"],
            row["executed_subject_id"],
            row["rejection_reason"],
            ProductAccountService._aware(row["created_at"]),
            ProductAccountService._aware(row["updated_at"]),
        )
