"""Audit GCS-stored generated audio for structural corruption.

Read-only health check over the chapter-audio artifacts in
``settings.AUDIO_BUCKET_NAME`` — the ``audio/`` (verse mode),
``audio_chapter/`` (chapter mode), ``audio_gemini/`` and
``audio_edge/`` prefixes. For every ``*_audio.mp3`` it validates
the MP3 frame stream (desynced/truncated bytes, absurd duration
or implied bitrate) and cross-checks the ``*_timestamps.json``
sidecar (size + duration agreement, monotonic in-range verse
offsets). Orphaned artifacts — an MP3 without a sidecar, or a
sidecar in a directory that holds MP3s but lacks this one — are
flagged too.

The command never writes: it only lists and downloads blobs.
Exit status is non-zero when suspects are found so a scheduled
run can alert on failure. Use ``--metadata-only`` for the
cheap pass (list + small sidecar JSONs, no MP3 downloads) —
that is the mode meant to stay inside free-tier limits on a
frequent schedule.

``--deep`` adds content-level checks (clipped samples, dead
silence, loudness outliers per verse segment) by decoding each
MP3 with ``miniaudio`` — a dev-only dependency, so this mode
is LOCAL-only and not free-tier safe; see DEVELOPER_GUIDE.md.
"""
import json
import logging

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from bible.services import audio_audit
from bible.services import audio_deep
from bible.services.storage import gcs

logger = logging.getLogger(__name__)

DEFAULT_PREFIXES = (
    "audio",
    "audio_chapter",
    "audio_gemini",
    "audio_edge",
)


class Command(BaseCommand):
    help = (
        "Audit GCS audio artifacts for corruption (read-only). "
        "Exits non-zero when suspects are found."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--prefix",
            default=",".join(DEFAULT_PREFIXES),
            help=(
                "Comma-separated bucket prefixes to scan "
                f"(default: {','.join(DEFAULT_PREFIXES)})."
            ),
        )
        parser.add_argument(
            "--fileset-id",
            help="Only blobs whose path contains /<ID>/.",
        )
        parser.add_argument(
            "--book",
            help="Only blobs whose path contains /<BOOK>/.",
        )
        parser.add_argument(
            "--limit", type=int, default=0,
            help="Fully validate at most N MP3s (0 = no limit).",
        )
        parser.add_argument(
            "--metadata-only", action="store_true",
            help=(
                "Skip MP3 downloads: only listing, pairing and "
                "sidecar-JSON consistency checks run. Cheapest "
                "mode — meant for frequent scheduled runs."
            ),
        )
        parser.add_argument(
            "--json", action="store_true", dest="json_output",
            help="Emit the report as JSON instead of text lines.",
        )
        parser.add_argument(
            "--verbose", action="store_true",
            help="Also print a line for every clean artifact.",
        )
        parser.add_argument(
            "--deep", action="store_true",
            help=(
                "Also decode every downloaded MP3 and check its "
                "content: per-verse clipping, dead silence and "
                "loudness outliers (dev-only decoder, local use "
                "only — not combinable with --metadata-only)."
            ),
        )
        parser.add_argument(
            "--clip-min-run", type=int,
            default=audio_deep.CLIP_MIN_RUN,
            help=(
                "Consecutive near-full-scale samples that flag a "
                "segment as clipped "
                f"(default: {audio_deep.CLIP_MIN_RUN})."
            ),
        )
        parser.add_argument(
            "--clip-min-total", type=int,
            default=audio_deep.CLIP_MIN_TOTAL,
            help=(
                "Total near-full-scale samples that flag a "
                "segment as clipped even without a long run "
                f"(default: {audio_deep.CLIP_MIN_TOTAL})."
            ),
        )

    def handle(self, *args, **options):
        prefixes = [
            p.strip().strip("/")
            for p in options["prefix"].split(",")
            if p.strip()
        ]
        if not prefixes:
            raise CommandError("--prefix resolved to an empty list")
        fileset_id = (options["fileset_id"] or "").upper() or None
        book = (options["book"] or "").upper() or None
        metadata_only = options["metadata_only"]
        limit = options["limit"]
        deep = options["deep"]
        if deep and metadata_only:
            raise CommandError(
                "--deep cannot be combined with --metadata-only "
                "(content checks need the MP3 downloads)"
            )
        if deep:
            # Fail fast with an actionable message before listing
            # a single blob when the dev-only decoder is absent.
            try:
                audio_deep.ensure_decoder()
            except audio_deep.DecoderUnavailableError as exc:
                raise CommandError(str(exc))

        bucket = gcs.get_default_client().bucket(
            settings.AUDIO_BUCKET_NAME
        )
        try:
            report = self._audit(
                bucket, prefixes, fileset_id, book,
                metadata_only=metadata_only, limit=limit,
                deep=deep,
                clip_min_run=options["clip_min_run"],
                clip_min_total=options["clip_min_total"],
            )
        except audio_deep.DecoderUnavailableError as exc:
            raise CommandError(str(exc))
        self._emit(report, options)
        if report["suspects"]:
            raise CommandError(
                f"{len(report['suspects'])} suspect artifact(s) "
                "found — see report above"
            )

    # ---------------------------------------------------------
    # Audit driver — kept GCS-shaped (blob objects) but trivially
    # fakeable in tests.
    # ---------------------------------------------------------

    def _audit(
        self, bucket, prefixes, fileset_id, book,
        metadata_only=False, limit=0, deep=False,
        clip_min_run=audio_deep.CLIP_MIN_RUN,
        clip_min_total=audio_deep.CLIP_MIN_TOTAL,
    ) -> dict:
        audio_blobs = {}
        sidecar_blobs = {}
        for prefix in prefixes:
            for blob in bucket.list_blobs(prefix=prefix + "/"):
                if not self._matches(blob.name, fileset_id, book):
                    continue
                if blob.name.endswith(audio_audit.AUDIO_SUFFIX):
                    audio_blobs[blob.name] = blob
                elif blob.name.endswith(audio_audit.TIMESTAMPS_SUFFIX):
                    sidecar_blobs[blob.name] = blob

        results = []
        checked = 0
        for name in sorted(audio_blobs):
            blob = audio_blobs[name]
            entry = {"name": name, "size": blob.size, "issues": []}
            sidecar_name = (
                name[: -len(audio_audit.AUDIO_SUFFIX)]
                + audio_audit.TIMESTAMPS_SUFFIX
            )
            sidecar = sidecar_blobs.get(sidecar_name)
            payload = None
            if sidecar is None:
                entry["issues"].append({
                    "code": "missing_sidecar",
                    "detail": f"no {sidecar_name}",
                })
            else:
                payload, ts_issues = audio_audit.audit_timestamps(
                    sidecar.download_as_bytes()
                )
                entry["issues"].extend(
                    {"code": i.code, "detail": i.detail}
                    for i in ts_issues
                )

            fully_checked = (
                not metadata_only
                and (not limit or checked < limit)
            )
            if fully_checked:
                checked += 1
                data = blob.download_as_bytes()
                check = audio_audit.audit_mp3(name, data)
                entry["duration_seconds"] = check.duration_seconds
                entry["issues"].extend(
                    {"code": i.code, "detail": i.detail}
                    for i in check.issues
                )
                if payload:
                    entry["issues"].extend(
                        {"code": i.code, "detail": i.detail}
                        for i in audio_audit.cross_check(
                            name, blob.size, check, payload,
                        )
                    )
                if deep:
                    entry["issues"].extend(
                        {"code": i.code, "detail": i.detail}
                        for i in self._deep_check(
                            data, payload,
                            clip_min_run=clip_min_run,
                            clip_min_total=clip_min_total,
                        )
                    )
            elif payload:
                # Metadata-only consistency: sidecar-declared
                # file size vs the listing's blob size. An empty
                # AudioCheck makes cross_check skip the
                # duration comparisons.
                stub = audio_audit.AudioCheck(
                    name=name, size=blob.size,
                )
                entry["issues"].extend(
                    {"code": i.code, "detail": i.detail}
                    for i in audio_audit.cross_check(
                        name, blob.size, stub, payload,
                    )
                )
            results.append(entry)

        # Sidecars are only orphans in directories that actually
        # hold MP3s — a directory of JSONs alone belongs to a
        # different artifact layout (e.g. timestamps_generated/).
        audio_dirs = {
            name.rsplit("/", 1)[0] for name in audio_blobs
        }
        orphans = sorted(
            name for name in sidecar_blobs
            if name.rsplit("/", 1)[0] in audio_dirs
            and (
                name[: -len(audio_audit.TIMESTAMPS_SUFFIX)]
                + audio_audit.AUDIO_SUFFIX
            ) not in audio_blobs
        )
        return {
            "bucket": settings.AUDIO_BUCKET_NAME,
            "prefixes": prefixes,
            "metadata_only": metadata_only,
            "deep": deep,
            "scanned": len(audio_blobs),
            "fully_checked": checked,
            "results": results,
            "orphan_sidecars": orphans,
            "suspects": [
                e["name"] for e in results if e["issues"]
            ] + orphans,
        }

    @staticmethod
    def _deep_check(
        data, payload, *, clip_min_run, clip_min_total,
    ):
        """Decode MP3 bytes and run the content checks.

        Decoder absence raises ``DecoderUnavailableError`` (the
        command turns it into a CommandError); a stream the
        decoder itself cannot parse becomes a ``decode_failed``
        issue rather than aborting the whole audit."""
        try:
            samples, sample_rate = audio_deep.decode_mp3_bytes(
                data
            )
        except audio_deep.DecoderUnavailableError:
            raise
        except Exception as exc:
            return [audio_audit.Issue(
                "decode_failed", exc.__class__.__name__,
            )]
        return audio_deep.analyze_pcm(
            samples, sample_rate, payload,
            clip_min_run=clip_min_run,
            clip_min_total=clip_min_total,
        )

    @staticmethod
    def _matches(name, fileset_id, book):
        if fileset_id and f"/{fileset_id}/" not in name:
            return False
        if book and f"/{book}/" not in name:
            return False
        return True

    # ---------------------------------------------------------
    # Reporting
    # ---------------------------------------------------------

    def _emit(self, report, options):
        if options["json_output"]:
            self.stdout.write(json.dumps(report, indent=2))
            return
        verbose = options["verbose"]
        for entry in report["results"]:
            if entry["issues"]:
                detail = "; ".join(
                    f"{i['code']}: {i['detail']}"
                    for i in entry["issues"]
                )
                self.stdout.write(
                    f"SUSPECT {entry['name']}  {detail}"
                )
            elif verbose:
                dur = entry.get("duration_seconds")
                dur_txt = (
                    f"{dur:.1f}s" if isinstance(dur, float)
                    else "not downloaded"
                )
                self.stdout.write(
                    f"OK      {entry['name']}  "
                    f"{entry['size']}B {dur_txt}"
                )
        for name in report["orphan_sidecars"]:
            self.stdout.write(
                f"SUSPECT {name}  orphan_sidecar: "
                "no matching _audio.mp3"
            )
        self.stdout.write(
            "summary: scanned={scanned} fully_checked={fc} "
            "suspects={n} metadata_only={mo} deep={d}".format(
                scanned=report["scanned"],
                fc=report["fully_checked"],
                n=len(report["suspects"]),
                mo=report["metadata_only"],
                d=report["deep"],
            )
        )
