"""Protocol-level xAI accounts signup (Connect/gRPC-Web) helpers.

Faster path for email OTP steps; CreateUser still typically needs Turnstile
(and optionally Castle request token) from a browser fingerprint.

Discovered service:
  POST https://accounts.x.ai/auth_mgmt.AuthManagement/<Method>
  content-type: application/grpc-web+proto
  x-user-agent: connect-es/2.1.1
"""

from __future__ import annotations

import json
import re
import secrets
import struct
import uuid
from typing import Any, Callable

from curl_cffi import requests

SERVICE = "https://accounts.x.ai/auth_mgmt.AuthManagement"
LogFn = Callable[[str], None]


def _noop(_: str) -> None:
    return None


def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            break
    return bytes(out)


def _encode_key(field_no: int, wire_type: int) -> bytes:
    return _varint((field_no << 3) | wire_type)


def encode_string(field_no: int, value: str) -> bytes:
    data = (value or "").encode("utf-8")
    return _encode_key(field_no, 2) + _varint(len(data)) + data


def encode_bytes(field_no: int, data: bytes) -> bytes:
    data = data or b""
    return _encode_key(field_no, 2) + _varint(len(data)) + data


def encode_varint_field(field_no: int, value: int) -> bytes:
    return _encode_key(field_no, 0) + _varint(int(value))


def encode_message(field_no: int, payload: bytes) -> bytes:
    return encode_bytes(field_no, payload)


def grpc_web_unary(payload: bytes) -> bytes:
    """gRPC-web / connect data frame: flags(0) + big-endian length + protobuf."""
    return b"\x00" + struct.pack(">I", len(payload)) + payload


def parse_grpc_web_response(raw: bytes) -> tuple[bytes, dict[str, str]]:
    """Return (message_bytes, trailers)."""
    data = raw or b""
    msg = b""
    trailers: dict[str, str] = {}
    i = 0
    while i + 5 <= len(data):
        flags = data[i]
        length = struct.unpack(">I", data[i + 1 : i + 5])[0]
        i += 5
        chunk = data[i : i + length]
        i += length
        if flags & 0x80:  # trailer
            text = chunk.decode("utf-8", errors="replace")
            for line in text.split("\r\n"):
                if ":" in line:
                    k, v = line.split(":", 1)
                    trailers[k.strip().lower()] = v.strip()
        else:
            msg = chunk
    return msg, trailers


class ProtocolXaiClient:
    def __init__(
        self,
        *,
        proxy: str | None = None,
        impersonate: str = "chrome131",
        log: LogFn | None = None,
    ):
        self.log = log or _noop
        self.impersonate = impersonate
        self.session = requests.Session()
        self.proxy = (proxy or "").strip() or None
        self._warmed = False

    def _proxies(self) -> dict | None:
        if not self.proxy:
            return None
        return {"http": self.proxy, "https": self.proxy}

    def warm(self) -> None:
        if self._warmed:
            return
        try:
            r = self.session.get(
                "https://accounts.x.ai/sign-up?redirect=grok-com",
                impersonate=self.impersonate,
                proxies=self._proxies(),
                timeout=12,
            )
            self.log(f"[protocol] warm status={r.status_code}")
        except Exception as e:
            # Warm is best-effort; RPC may still work without it
            self.log(f"[protocol] warm skipped: {e}")
        self._warmed = True

    def _headers(self, content_type: str) -> dict[str, str]:
        return {
            "content-type": content_type,
            "origin": "https://accounts.x.ai",
            "referer": "https://accounts.x.ai/sign-up?redirect=grok-com",
            "accept": "*/*",
            "x-grpc-web": "1",
            "x-user-agent": "connect-es/2.1.1",
        }

    def call_proto(self, method: str, payload: bytes) -> dict[str, Any]:
        self.warm()
        url = f"{SERVICE}/{method}"
        body = grpc_web_unary(payload)
        r = self.session.post(
            url,
            headers=self._headers("application/grpc-web+proto"),
            data=body,
            impersonate=self.impersonate,
            proxies=self._proxies(),
            timeout=30,
        )
        msg, trailers = parse_grpc_web_response(r.content or b"")
        status = trailers.get("grpc-status", "")
        message = trailers.get("grpc-message", "")
        # Real gRPC-web success always returns trailer grpc-status:0.
        # Empty body with no trailers is NOT success (CF/gateway noise).
        ok = r.status_code == 200 and status == "0"
        self.log(
            f"[protocol] {method} http={r.status_code} grpc_status={status or '(none)'} "
            f"msg_len={len(msg)} trailers={trailers or {}}"
        )
        return {
            "ok": ok,
            "http_status": r.status_code,
            "grpc_status": status,
            "grpc_message": message,
            "message": msg,
            "trailers": trailers,
            "raw": r.content,
            "set_cookie": r.headers.get("set-cookie", ""),
            "cookies": dict(self.session.cookies),
        }

    def create_email_validation_code(
        self, email: str, castle_request_token: str = ""
    ) -> dict[str, Any]:
        """CreateEmailValidationCode.

        Captured browser layout:
          field 1 = email
          field 3 = castleRequestToken (large, optional for send in practice)
        """
        payload = encode_string(1, email)
        if castle_request_token:
            payload += encode_string(3, castle_request_token)
        return self.call_proto("CreateEmailValidationCode", payload)

    @staticmethod
    def normalize_email_code(code: str) -> str:
        """Browser sends OTP without dashes: UN1-MHY -> UN1MHY."""
        return re.sub(r"[^A-Za-z0-9]", "", str(code or "")).upper()

    def verify_email_validation_code(self, email: str, code: str) -> dict[str, Any]:
        """VerifyEmailValidationCode.

        Captured browser layout:
          field 1 = email
          field 2 = emailValidationCode (NO dashes)
        """
        code_n = self.normalize_email_code(code)
        payload = encode_string(1, email) + encode_string(2, code_n)
        return self.call_proto("VerifyEmailValidationCode", payload)

    def validate_password(self, email: str, password: str) -> dict[str, Any]:
        """ValidatePassword — captured: field4=email, field5=password."""
        payload = encode_string(4, email) + encode_string(5, password)
        return self.call_proto("ValidatePassword", payload)

    def create_user_and_session_v2(
        self,
        *,
        email: str,
        code: str,
        given_name: str,
        family_name: str,
        password: str,
        turnstile_token: str,
        tos_accepted_version: int | None = 1,
        conversion_id: str | None = None,
        castle_request_token: str = "",
    ) -> dict[str, Any]:
        """CreateUserAndSessionV2 nested request.

        JSON shape (from live browser POST /sign-up transport):
          {
            emailValidationCode,  # no dashes
            createUserAndSessionRequest: {
              email, givenName, familyName, clearTextPassword, tosAcceptedVersion
            },
            turnstileToken, conversionId, castleRequestToken
          }
        Field numbers follow typical proto3 declaration order (best-effort).
        """
        code_n = self.normalize_email_code(code)
        inner = b"".join(
            [
                encode_string(1, email),
                encode_string(2, given_name),
                encode_string(3, family_name),
                encode_string(4, password),
            ]
        )
        if tos_accepted_version is not None:
            inner += encode_varint_field(5, int(tos_accepted_version))

        outer = b"".join(
            [
                encode_string(1, code_n),
                encode_message(2, inner),
                encode_string(3, turnstile_token or ""),
                encode_string(4, conversion_id or str(uuid.uuid4())),
            ]
        )
        if castle_request_token:
            outer += encode_string(5, castle_request_token)
        return self.call_proto("CreateUserAndSessionV2", outer)

    def create_user_via_signup_action(
        self,
        *,
        email: str,
        code: str,
        given_name: str,
        family_name: str,
        password: str,
        turnstile_token: str,
        conversion_id: str | None = None,
        castle_request_token: str = "",
        tos_accepted_version: int = 1,
        next_action: str = "",
        next_router_state_tree: str = "",
    ) -> dict[str, Any]:
        """Create user via Next.js Server Action (live capture 2026-07).

        Browser posts:
          POST /sign-up?redirect=grok-com
          content-type: text/plain;charset=UTF-8
          accept: text/x-component
          next-action: <40-hex build-id action hash>
          body: [ {createUser payload}, {client:'$T', ...} ]

        NOTE: ``next-action`` changes when the frontend is redeployed; capture it
        from a live browser request header when possible.
        """
        self.warm()
        code_n = self.normalize_email_code(code)
        payload = [
            {
                "emailValidationCode": code_n,
                "createUserAndSessionRequest": {
                    "email": email,
                    "givenName": given_name,
                    "familyName": family_name,
                    "clearTextPassword": password,
                    "tosAcceptedVersion": int(tos_accepted_version),
                },
                "turnstileToken": turnstile_token or "",
                "conversionId": conversion_id or str(uuid.uuid4()),
                "castleRequestToken": castle_request_token or "",
            },
            {
                "client": "$T",
                "meta": "$undefined",
                "mutationKey": "$undefined",
            },
        ]
        # Last known live action id (may rotate on deploy)
        action = (next_action or "").strip() or (
            "7f50061dd2f5b389a530e4a048d5fdf0c48d1d9259"
        )
        tree = (next_router_state_tree or "").strip() or (
            "%5B%22%22%2C%7B%22children%22%3A%5B%22(app)%22%2C%7B%22children%22%3A%5B%22(auth)%22%2C%7B%22children%22%3A%5B%22sign-up%22%2C%7B%22children%22%3A%5B%22__PAGE__%22%2C%7B%7D%2Cnull%2Cnull%5D%7D%5D%7D%5D%7D%5D%2Cnull%2Cnull%2Ctrue%5D"
        )
        url = "https://accounts.x.ai/sign-up?redirect=grok-com"
        headers = {
            "content-type": "text/plain;charset=UTF-8",
            "origin": "https://accounts.x.ai",
            "referer": "https://accounts.x.ai/sign-up?redirect=grok-com",
            "accept": "text/x-component",
            "next-action": action,
            "next-router-state-tree": tree,
        }
        body = json.dumps(payload, separators=(",", ":"))
        r = self.session.post(
            url,
            headers=headers,
            data=body,
            impersonate=self.impersonate,
            proxies=self._proxies(),
            timeout=45,
        )
        sso = self.extract_sso_from_cookies()
        set_cookie = r.headers.get("set-cookie", "") or ""
        if not sso and "sso=" in set_cookie:
            m = re.search(r"\bsso=([^;]+)", set_cookie)
            if m:
                sso = m.group(1)
        self.log(
            f"[protocol] server-action http={r.status_code} len={len(r.content or b'')} "
            f"action={action[:12]}… sso={'yes' if sso else 'no'}"
        )
        return {
            "ok": bool(sso),
            "http_status": r.status_code,
            "text": (r.text or "")[:500],
            "sso": sso,
            "set_cookie": set_cookie[:300],
            "cookies": dict(self.session.cookies),
            "next_action": action,
        }

    def extract_sso_from_cookies(self) -> str:
        for name in ("sso", "sso-rw"):
            val = self.session.cookies.get(name)
            if val:
                return str(val)
        return ""


def random_password() -> str:
    return "N" + secrets.token_hex(4) + "!a7#" + secrets.token_urlsafe(6)


def random_name() -> tuple[str, str]:
    given = [
        "Neo",
        "Ethan",
        "Liam",
        "Noah",
        "Lucas",
        "Ryan",
        "Leo",
        "Owen",
        "Aiden",
        "Kai",
    ]
    family = ["Lin", "Wang", "Chen", "Zhang", "Liu", "Yang", "Wu", "Huang", "Zhao", "Xu"]
    return secrets.choice(given), secrets.choice(family)
