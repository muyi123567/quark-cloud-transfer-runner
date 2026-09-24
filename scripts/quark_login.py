#!/usr/bin/env python3
"""One-time Quark Web QR login helper.

The runtime never needs this script. It exists only to create the cookie that
is then stored as the GitHub Actions secret QUARK_COOKIE.
"""

from __future__ import annotations

import argparse
import sys
import time
import uuid
from pathlib import Path

import qrcode
import requests
from qrcode.constants import ERROR_CORRECT_M

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a Quark Web cookie by QR login.")
    parser.add_argument(
        "--output",
        default=".quark_cookie.txt",
        help="Credential output path. Add it to .gitignore and upload it as QUARK_COOKIE.",
    )
    parser.add_argument("--timeout-seconds", type=int, default=600)
    args = parser.parse_args()

    output = Path(args.output).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    qr_path = output.with_suffix(".png")

    session = requests.Session()
    session.trust_env = False
    session.headers.update(
        {
            "User-Agent": UA,
            "Referer": "https://pan.quark.cn/",
            "Accept": "application/json, text/plain, */*",
        }
    )

    token_response = session.get(
        "https://uop.quark.cn/cas/ajax/getTokenForQrcodeLogin",
        params={"client_id": "532", "v": "1.2", "request_id": str(uuid.uuid4())},
        timeout=30,
    )
    token_response.raise_for_status()
    token = token_response.json()["data"]["members"]["token"]

    uc_biz = "S:custom|OPT:SAREA@0|OPT:IMMERSIVE@1|OPT:BACK_BTN_STYLE@0"
    qr_url = (
        "https://su.quark.cn/4_eMHBJ?token="
        + requests.utils.quote(token, safe="")
        + "&client_id=532&ssb=weblogin&uc_param_str=&uc_biz_str="
        + requests.utils.quote(uc_biz, safe="")
    )
    qr = qrcode.QRCode(error_correction=ERROR_CORRECT_M, box_size=8, border=2)
    qr.add_data(qr_url)
    qr.make(fit=True)
    qr.make_image(fill_color="black", back_color="white").save(qr_path)

    print("QUARK_LOGIN_REQUIRED")
    print(f"Scan this QR image with the Quark app: {qr_path}")
    try:
        qr.print_ascii(invert=True)
    except Exception:
        pass
    print("Waiting for authorization. The cookie value will never be printed.", flush=True)

    ticket = None
    attempts = max(1, args.timeout_seconds // 2)
    for index in range(attempts):
        time.sleep(2)
        try:
            members = (
                session.get(
                    "https://uop.quark.cn/cas/ajax/getServiceTicketByQrcodeToken",
                    params={
                        "client_id": "532",
                        "v": "1.2",
                        "token": token,
                        "request_id": str(uuid.uuid4()),
                    },
                    timeout=20,
                )
                .json()
                .get("data", {})
                .get("members", {})
            )
        except Exception:
            continue
        if members.get("service_ticket"):
            ticket = str(members["service_ticket"])
            print(f"Quark authorization confirmed after {(index + 1) * 2}s.")
            break
        if (index + 1) % 30 == 0:
            print(f"Still waiting: {(index + 1) * 2}s", flush=True)

    if not ticket:
        print("ERROR: QR authorization timed out. Run the helper again.", file=sys.stderr)
        return 2

    auth_session = requests.Session()
    auth_session.trust_env = False
    auth_session.headers.update(
        {"User-Agent": UA, "Referer": "https://pan.quark.cn/"}
    )
    auth_session.get(
        "https://pan.quark.cn/account/info?st="
        + requests.utils.quote(ticket, safe="")
        + "&lw=scan",
        timeout=30,
        allow_redirects=True,
    )
    for url in [
        "https://pan.quark.cn/list",
        (
            "https://drive-pc.quark.cn/1/clouddrive/file/sort"
            "?pr=ucpro&fr=pc&uc_param_str=&pdir_fid=0&_page=1&_size=50"
            "&_fetch_total=1&_sort=file_type:asc,updated_at:desc"
        ),
        (
            "https://drive.quark.cn/1/clouddrive/member"
            "?pr=ucpro&fr=pc&uc_param_str=&fetch_subscribe=true"
        ),
    ]:
        try:
            auth_session.get(url, timeout=30, allow_redirects=True)
        except requests.RequestException:
            pass

    cookie = "; ".join(
        f"{cookie.name}={cookie.value}" for cookie in auth_session.cookies
    )
    if "__puus=" not in cookie:
        print("ERROR: Login completed but __puus was not established.", file=sys.stderr)
        return 3
    output.write_text(cookie, encoding="utf-8")
    print("QUARK_LOGIN_OK")
    print(f"Credential written to: {output}")
    print("Next: upload it with `gh secret set QUARK_COOKIE < .quark_cookie.txt`.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
