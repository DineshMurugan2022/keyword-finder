import hashlib
import html
import json
from pathlib import Path


def save_json(path: Path, value: dict | list) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def file_record(path: Path) -> dict:
    return {"file": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "bytes": path.stat().st_size}


def write_report(directory: Path, observation: dict) -> None:
    def esc(value: object) -> str:
        return html.escape(str(value))
    rows = "".join(
        f"<tr><td>{r['position']}</td><td>{esc(r['title'])}</td><td>{esc(r['url'])}</td></tr>"
        for r in observation["results"]
    )
    pictures = "".join(
        f'<figure><a href="{esc(item["file"])}"><img src="{esc(item["file"])}" '
        f'alt="Captured results page" loading="lazy"></a><figcaption>{esc(item["file"])}</figcaption></figure>'
        for item in observation["evidence"] if item["file"].endswith(".png")
    )
    label = "LOCAL DEMO — NOT A GOOGLE RANKING" if observation["source"] == "fixture" else "Chrome ranking observation"
    document = f"""<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Ranking evidence</title><style>
body{{font:16px system-ui;background:#f4f6fa;color:#17233c;max-width:1050px;margin:40px auto;padding:20px}}
article{{background:white;border-radius:16px;padding:28px;margin:18px 0}}h1{{font-size:32px}}
table{{width:100%;border-collapse:collapse}}td,th{{text-align:left;padding:12px;border-bottom:1px solid #ddd;overflow-wrap:anywhere}}
img{{width:100%;border:1px solid #ddd}}figure{{margin:24px 0}}code{{overflow-wrap:anywhere}}
</style><h1>{label}</h1><article><h2>{esc(observation['request']['keyword'])}</h2>
<p>Website: <b>{esc(observation['request']['website'])}</b></p>
<p>Status: <b>{esc(observation['status'])}</b> · Organic rank: <b>{esc(observation.get('organic_rank'))}</b></p>
<p>{esc(observation['message'])}</p><p>{esc(observation['checked_at'])} · {esc(observation['run_id'])}</p>
<p>Requested location: {esc(observation['request']['location'] or observation['request']['country'])}.
Location verified: {esc(observation['location_verified'])}. Desktop Chrome, signed out.</p>
<p><a href="observation.json">Observation JSON</a> · <a href="manifest.json">Evidence hashes</a></p></article>
<article><h2>Observed organic results</h2><table><tr><th>Position</th><th>Title</th><th>URL</th></tr>{rows}</table></article>
<article><h2>Original screenshots</h2>{pictures}</article></html>"""
    (directory / "report.html").write_text(document, encoding="utf-8")


def finalize(directory: Path, observation: dict) -> dict:
    save_json(directory / "observation.json", observation)
    write_report(directory, observation)
    # Build the file list explicitly from known, already-written artefacts
    # instead of iterating the directory, which avoids a race condition if
    # another process writes files concurrently.
    known_names = (
        ["observation.json", "report.html"]
        + [e["file"] for e in observation.get("evidence", [])]
    )
    seen: set[str] = set()
    file_records: list[dict] = []
    for name in known_names:
        if name in seen:
            continue
        seen.add(name)
        path = directory / name
        if path.is_file():
            file_records.append(file_record(path))
    save_json(directory / "manifest.json", {"run_id": observation["run_id"], "files": file_records})
    return observation


def verify(directory: Path) -> bool:
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    for entry in manifest["files"]:
        path = directory / entry["file"]
        if path.parent.resolve() != directory.resolve() or not path.is_file():
            return False
        if file_record(path) != entry:
            return False
    return True


def upload_to_s3(directory: Path, bucket: str, prefix: str) -> None:
    """Upload all evidence files to S3. Requires boto3; silently skips if not installed.

    Files are uploaded to s3://<bucket>/<prefix>/<run_id>/<filename>.
    Never raises — caller is responsible for logging failures.
    """
    try:
        import boto3  # noqa: PLC0415
        from botocore.exceptions import ClientError  # noqa: PLC0415
    except ImportError:
        import logging
        logging.getLogger(__name__).warning(
            "boto3 is not installed; skipping S3 upload. "
            "Install it with: pip install boto3"
        )
        return

    import logging
    log = logging.getLogger(__name__)
    client = boto3.client("s3")
    run_id = directory.name  # evidence directories are named by run_id
    for path in sorted(directory.iterdir()):
        if not path.is_file():
            continue
        key = f"{prefix.rstrip('/')}/{run_id}/{path.name}"
        try:
            client.upload_file(str(path), bucket, key)
            log.info("S3 uploaded s3://%s/%s", bucket, key)
        except ClientError as exc:
            log.error("S3 upload failed for %s: %s", path.name, exc)
            raise

