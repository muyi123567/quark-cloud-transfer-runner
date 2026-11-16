import base64
import hashlib
import io
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from quark_cloud_transfer.errors import TransferError
from quark_cloud_transfer.models import QuarkItem
from quark_cloud_transfer.sites_r2 import SitesR2, PART_SIZE


def sha(data):
    return hashlib.sha256(data).hexdigest()


def test_sites_http_resume_and_download(tmp_path):
    data = b"a" * PART_SIZE + b"bc"
    digest = sha(data)
    key = "files/example"
    methods = []
    offsets = []
    fail_at = [256 * 1024]

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            assert self.headers["Authorization"] == "Bearer test-token"
            call = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            name = call["params"]["name"]
            args = call["params"]["arguments"]
            methods.append(name)
            if name == "r2_api_init":
                assert args["partHashes"] == [sha(data[:PART_SIZE]), sha(b"bc")]
                payload = {"id": args["id"], "key": key, "status": "open",
                           "partCount": 2, "partSize": PART_SIZE}
            elif name == "r2_api_status":
                payload = {"id": args["id"], "status": "open", "present": [1], "next": None}
            elif name == "r2_api_part":
                assert args["number"] == 2
                assert base64.b64decode(args["base64"]) == b"bc"
                payload = {"id": args["id"], "status": "created", "partNumber": 2}
            elif name == "r2_api_commit":
                payload = {"id": args["id"], "status": "committed"}
            else:
                assert name == "r2_api_download"
                offset = args["offset"]
                offsets.append(offset)
                if fail_at and offset == fail_at[0]:
                    fail_at.pop()
                    self.send_response(403)
                    self.end_headers()
                    return
                block = data[offset:offset + 128 * 1024]
                end = offset + len(block)
                payload = {"key": key, "size": len(data), "offset": offset,
                           "length": len(block), "sha256": digest,
                           "next": end if end < len(data) else None,
                           "base64": base64.b64encode(block).decode()}
            result = {"jsonrpc": "2.0", "id": call["id"],
                      "result": {"content": [{"type": "text", "text": json.dumps(payload)}]}}
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(result).encode())

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            adapter = SitesR2(f"http://127.0.0.1:{server.server_port}/mcp", "test-token")
            item = QuarkItem("fid", "example", "/example", len(data), False)
            result = adapter.upload(item, "target", io.BytesIO(data))
            assert (result.key, result.sha256) == (key, digest)
            output = tmp_path / "example"
            with pytest.raises(TransferError, match="HTTP 403"):
                adapter.download(key, output)
            assert not output.exists()
            assert next(tmp_path.iterdir()).stat().st_size == 256 * 1024
            adapter.download(key, output)
            assert offsets.count(128 * 1024) == 1
            assert output.read_bytes() == data
            with pytest.raises(TransferError):
                adapter.download(key, output)
            assert methods.count("r2_api_part") == 1
        finally:
            server.shutdown()
            worker.join()


def test_sites_cli_transfers_without_drive_and_preserves_fid(monkeypatch, capsys):
    from quark_cloud_transfer import cli
    from quark_cloud_transfer.storage import StoredObject

    data = b"abc"
    fingerprint = sha(data)

    class Quark:
        def __init__(self, *args, **kwargs):
            pass

        def get_download_items(self, fids):
            assert fids == ["original-fid"]
            return [{"download_url": "https://example.invalid/file", "size": 3, "file_name": "example"}]

        def probe_range(self, url, size):
            return True

        def fetch_range(self, url, start, end):
            return data[start:end + 1]

    class Adapter:
        def upload(self, item, destination, source):
            assert item.fid == "original-fid"
            assert destination == "27考研/数学"
            source.seek(0)
            assert source.read() == data
            return StoredObject("files/example", len(data), fingerprint)

    def reject_drive(*args, **kwargs):
        pytest.fail("Drive client constructed in Sites R2 mode")

    monkeypatch.setenv("QUARK_COOKIE", "__puus=test")
    monkeypatch.setenv("GDRIVE_OAUTH_JSON", "invalid json")
    monkeypatch.setattr(cli, "QuarkClient", Quark)
    monkeypatch.setattr(cli, "GoogleDriveClient", reject_drive)
    monkeypatch.setattr(cli, "_sites_client", Adapter)
    assert cli.main([
        "--backend", "sites-r2", "--source-fid", "original-fid", "--destination", "27考研/数学",
    ]) == 0
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    receipt = next(event for event in events if event["event"] == "storage_verified")
    assert receipt["source_fid"] == "original-fid"
    assert receipt["object_key"] == "files/example"
    assert receipt["sha256"] == fingerprint
    assert receipt["verified_at"].endswith("+00:00")
