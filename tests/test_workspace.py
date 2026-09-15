import asyncio

import httpx

from coifesp_harness.control_plane import create_app
from test_control_plane import StubVerifier, settings


async def request(app, path):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="https://control.example.test",
    ) as client:
        return await client.get(path)


def workspace_settings():
    return settings().__class__.from_environment(
        {
            "COIFESP_ENV": "test",
            "COIFESP_OIDC_ISSUER": "https://identity.example.test/realms/coifesp",
            "COIFESP_OIDC_AUDIENCE": "coifesp-control-plane",
            "COIFESP_OIDC_AUTHORIZED_PARTIES": "coifesp-local-ui",
            "COIFESP_UI_OIDC_CLIENT_ID": "coifesp-local-ui",
        }
    )


def local_workspace_settings():
    return settings().__class__.from_environment(
        {
            "COIFESP_ENV": "development",
            "COIFESP_AUTH_MODE": "local",
        }
    )


def test_workspace_is_same_origin_and_has_restrictive_browser_headers():
    app = create_app(settings=workspace_settings(), verifier=StubVerifier({}))
    page = asyncio.run(request(app, "/app/"))
    script = asyncio.run(request(app, "/app/app.js"))
    style = asyncio.run(request(app, "/app/app.css"))
    workbench_style = asyncio.run(request(app, "/app/agent-workbench.css"))
    resource_style = asyncio.run(request(app, "/app/project-resources.css"))
    blue_theme_style = asyncio.run(request(app, "/app/blue-theme.css"))
    assert (
        page.status_code
        == script.status_code
        == style.status_code
        == workbench_style.status_code
        == resource_style.status_code
        == blue_theme_style.status_code
        == 200
    )
    assert "COIFESP 协作工作台" in page.text
    assert "/app/blue-theme.css" in page.text
    assert "--green: #1769c2" in blue_theme_style.text
    assert "unsafe-inline" not in page.headers["content-security-policy"]
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    assert page.headers["cache-control"] == "no-store"
    assert "Agent 会话" in page.text
    assert "/conversation" in script.text
    assert "control-commands" in script.text
    assert "交付物" in page.text
    assert "/v1/artifacts:upload" in script.text
    assert "/resources:upload" in script.text
    assert "previewProjectResource" in script.text
    assert "协作收件箱" in page.text
    assert "/v1/collaboration-inbox" in script.text
    assert "整理收件箱 Agent" in page.text
    assert "inbox_agent_mode" in script.text
    assert "/v1/collaboration-inbox/agent-runs" in script.text
    assert "team-directory-search" in page.text
    assert "/v1/team-directory" in script.text
    assert "目标团队标识" not in script.text
    assert "chooseTaskArtifacts" in script.text


def test_workspace_assets_expose_task_scheduling_and_notification_center():
    app = create_app(settings=workspace_settings(), verifier=StubVerifier({}))
    page = asyncio.run(request(app, "/app/"))
    script = asyncio.run(request(app, "/app/app.js"))
    assert page.status_code == script.status_code == 200
    # Notification center navigation, tabs and preference entry are real UI.
    assert "通知中心" in page.text
    assert 'data-view="notifications"' in page.text
    assert "notification-unread-count" in page.text
    assert "data-notification-tab" in page.text
    assert "notification-category" in page.text
    assert "notification-project" in page.text
    assert "notification-preferences-button" in page.text
    assert "/v1/notifications" in script.text
    assert "/v1/notification-preferences" in script.text
    assert "data-notification-action" in script.text
    assert "notificationAction" in script.text
    assert ":mark-page-read" in script.text
    assert "markNotificationPageRead" in script.text
    assert "openNotificationPreferences" in script.text
    assert "renderNotificationProjectOptions" in script.text
    assert "openNotificationProject" in script.text
    assert "业务对象所在项目已不可用" in script.text
    # Task scheduling: creation form, filters, direct edits and proposals.
    assert "data-inbox-filter" in page.text
    assert "未设置截止时间" in script.text
    assert "PRIORITY_LABELS" in script.text
    assert "调整期限" in script.text
    assert "method:'PATCH'" in script.text
    assert "schedule-proposals" in script.text
    assert "decideScheduleProposal" in script.text
    assert "scheduleProposalCard" in script.text
    assert "expected_schedule_version" in script.text
    assert "expected_proposal_version" in script.text
    assert "due_within_hours" in script.text
    assert "overdue_only" in script.text
    assert "formatDue" in script.text


def test_workspace_assets_expose_agent_tools_and_skills():
    app = create_app(settings=workspace_settings(), verifier=StubVerifier({}))
    page = asyncio.run(request(app, "/app/"))
    script = asyncio.run(request(app, "/app/app.js"))
    assert page.status_code == script.status_code == 200
    # Capability query: real tool/skill availability is loaded before Agent
    # creation and shown in the picker.
    assert "/v1/agent-capabilities" in script.text
    assert "loadAgentCapabilities" in script.text
    assert "capabilityPickerHtml" in script.text
    assert "collectCapabilitySelection" in script.text
    assert "tool_ids" in script.text
    assert "skill_refs" in script.text
    assert "TOOL_LABELS" in script.text
    # Unavailable capabilities are visibly explained, never faked as usable.
    assert "需要审批" in script.text
    assert "未配置" in script.text
    assert "无权限" in script.text
    assert "只读" in script.text
    # Skills are opt-in, version-pinned and listed from the signed catalog
    # (surfaced through the capability query, not a separate skill browse).
    assert "默认不选择" in script.text
    assert "版本固定" in script.text
    assert "publisher_team" in script.text
    assert "required_tools.length" in script.text
    assert "无工具依赖" in script.text
    # The workbench loads and renders the immutable run authorization.
    assert "/authorizations" in script.text
    assert "renderRunAuthorizations" in script.text
    assert "本次运行授权" in script.text
    assert "授权在运行创建时固定，创建后不能扩大" in script.text
    # No secrets or internal configuration leak into the browser asset.
    assert "Bearer" in script.text  # sessions are attached at request time
    assert "secret" not in script.text.lower()
    assert "master_key" not in script.text.lower()
    assert "system_prompt" not in script.text.lower()
    assert "trusted_keys" not in script.text.lower()


def test_workspace_assets_expose_code_and_document_workspaces():
    app = create_app(settings=workspace_settings(), verifier=StubVerifier({}))
    page = asyncio.run(request(app, "/app/"))
    script = asyncio.run(request(app, "/app/app.js"))
    assert page.status_code == script.status_code == 200
    # Code workspace: binding, review drafts and explicit human confirmation.
    assert "代码工作区" in script.text
    assert "openCodeWorkspace" in script.text
    assert "data-code-select" in script.text
    assert "agentRepositoryContext" in script.text
    assert "patch_artifact_id" in script.text
    assert "确认应用" in script.text
    assert "拒绝" in script.text
    assert "尚未绑定仓库" in script.text
    assert "完整 Diff" in script.text
    assert "下载 Patch" in script.text
    # Document workspace: parse, versions, derivatives and drafts.
    assert "文档工作区" in script.text
    assert "openDocumentWorkspace" in script.text
    assert "解析当前文档" in script.text
    assert "data-doc-fragment" in script.text
    assert "agentDocumentContext" in script.text
    assert "data-doc-create-draft" in script.text
    # The document button only appears for office/pdf media types.
    assert "data-project-doc" in script.text
    assert 'x.media_type.includes("pdf")' in script.text


def test_workspace_exposes_only_public_oidc_configuration():
    app = create_app(settings=workspace_settings(), verifier=StubVerifier({}))
    response = asyncio.run(request(app, "/app/config"))
    assert response.status_code == 200
    assert response.json() == {
        "auth_mode": "oidc",
        "issuer": "https://identity.example.test/realms/coifesp",
        "client_id": "coifesp-local-ui",
        "audience": "coifesp-control-plane",
        "redirect_uri": "https://control.example.test/app/",
        # No session lifecycle service is wired in this minimal app; the
        # browser degrades to local logout instead of an IdP end-session.
        "end_session_endpoint": None,
        "post_logout_redirect_uri": "https://control.example.test/app/",
    }
    assert "secret" not in response.text.lower()


def test_workspace_config_fails_closed_without_public_client():
    app = create_app(settings=settings(), verifier=StubVerifier({}))
    response = asyncio.run(request(app, "/app/config"))
    assert response.status_code == 409
    assert response.json()["title"] == "Operation rejected"


def test_local_workspace_profiles_issue_real_bearer_sessions():
    app = create_app(settings=local_workspace_settings())

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url="https://control.example.test",
        ) as client:
            config = await client.get("/app/config")
            login = await client.post("/app/local-session", json={"profile_id": "lead"})
            identity = await client.get(
                "/v1/auth/me",
                headers={"Authorization": f'Bearer {login.json()["access_token"]}'},
            )
            return config, login, identity

    config, login, identity = asyncio.run(scenario())
    assert config.status_code == login.status_code == identity.status_code == 200
    assert config.json()["auth_mode"] == "local"
    assert [item["profile_id"] for item in config.json()["profiles"]] == [
        "lead",
        "contributor",
        "reviewer",
    ]
    assert identity.json()["principal_id"] == "lead-lin"
    assert identity.json()["tenant_id"] == "team-product"
    assert "collaboration_creator" in identity.json()["roles"]
    assert "artifact_publisher" in identity.json()["roles"]


def test_production_rejects_local_authentication_mode():
    values = {
        "COIFESP_ENV": "production",
        "COIFESP_AUTH_MODE": "local",
        "COIFESP_DATABASE_URL": "postgresql+psycopg://user:password@db.example/coifesp",
        "COIFESP_AUDIT_KEY_ID": "production-v1",
        "COIFESP_AUDIT_SIGNING_KEY": "a" * 32,
        "COIFESP_ENVELOPE_SIGNING_KEY": "b" * 32,
        "COIFESP_MEMORY_KEY_ID": "production-v1",
        "COIFESP_MEMORY_MASTER_KEY": "bW1tbW1tbW1tbW1tbW1tbW1tbW1tbW1tbW1tbW1tbW0=",
    }
    configured = settings().__class__.from_environment(values)
    import pytest
    from coifesp_harness.config import ConfigurationError

    with pytest.raises(ConfigurationError, match="AUTH_MODE"):
        configured.validate(require_auth=True)


def test_workspace_assets_expose_session_renewal_logout_and_route_recovery():
    app = create_app(settings=workspace_settings(), verifier=StubVerifier({}))
    page = asyncio.run(request(app, "/app/"))
    script = asyncio.run(request(app, "/app/app.js"))
    session = asyncio.run(request(app, "/app/session.js"))
    silent_page = asyncio.run(request(app, "/app/silent-callback.html"))
    silent_script = asyncio.run(request(app, "/app/silent-callback.js"))
    assert page.status_code == script.status_code == session.status_code == 200
    assert silent_page.status_code == silent_script.status_code == 200
    # index.html loads the coordinator before app.js.
    assert "/app/session.js" in page.text
    assert "session.js" in page.text
    assert "app.js" in page.text
    # app.js routes 401 responses through the session coordinator.
    assert "sessionHandleUnauthorized" in script.text
    assert "onUnauthorized" in script.text
    assert "createCoordinator" in script.text
    assert "defaultRenew" in script.text
    assert "sessionAttachAndRestore" in script.text
    assert "restoreRoute" in script.text
    assert "saveRoute" in script.text
    assert "fallbackLogout" in script.text
    assert "coifesp_login_hint" in script.text
    assert 'scope:"openid"' in script.text
    assert "openid profile email" not in script.text
    assert 'scope: "openid"' in session.text
    assert "openid profile email" not in session.text
    assert "Last-Event-ID" in script.text
    assert "eventCursors" in script.text
    # Broadcast messages carry only event metadata, never tokens.
    assert 'postMessage({ type: "renewed"' in session.text
    assert 'postMessage({ type: "logout"' in session.text
    # Silent callback validates state and posts the code to the same origin.
    assert "silent_state" in silent_script.text
    assert "coifesp:silent" in silent_script.text
    assert "parent.postMessage" in silent_script.text
    assert "location.origin" in silent_script.text
    # The connector status panel is real UI wired to the real API.
    assert "connector-panel" in page.text
    assert "connector-panel" in script.text
    assert "/v1/connectors/available" in script.text
    assert "renderConnectorPanel" in script.text
    assert "尚未配置外部连接器" in script.text


def test_workspace_silent_callback_page_is_frameable_only_by_same_origin():
    app = create_app(settings=workspace_settings(), verifier=StubVerifier({}))
    silent_page = asyncio.run(request(app, "/app/silent-callback.html"))
    page = asyncio.run(request(app, "/app/"))
    assert silent_page.status_code == 200
    assert "frame-ancestors 'self'" in silent_page.headers["content-security-policy"]
    assert "frame-ancestors 'none'" not in silent_page.headers["content-security-policy"]
    assert silent_page.headers["x-frame-options"] == "SAMEORIGIN"
    # The main workspace page stays locked down against embedding.
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    assert page.headers["x-frame-options"] == "DENY"
