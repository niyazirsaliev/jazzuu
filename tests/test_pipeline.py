"""Tests for the durable pipeline: SQLite is the source of truth, wakeups are not.

The production shape this file pins down:

    PLAUD reconciliation poll (~5 min)  <- the ONLY schedule in the system
      -> download to temp -> atomic rename into the archive
      -> recording upsert + ASR job enqueue, one transaction
      -> immediate worker wake -> ASR from LOCAL audio
      -> atomic selected transcript + FTS
      -> summary job -> mind-map job -> card job -> ready

Everything after the poll is driven by rows in `pipeline_jobs`. A wakeup — a
direct call, an event, an internal HTTP signal — only tells a worker to look at
the table sooner; losing every wakeup in the system may delay work, but must
never lose it. That is the property most of these tests are about.
"""
import importlib.util
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


def load_module(filename, module_name, env=None):
    """Import an archive/ module under a controlled environment.

    Same contract as tests/test_asr_quality.py: these modules resolve paths and
    credentials at import time, so `env` is the only way to see what a tenant
    container would really do. None removes a variable the host may export.
    """
    path = ROOT / "archive" / filename
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    env = env or {}
    with mock.patch.dict(sys.modules, {module_name: module}):
        with mock.patch.dict(
                os.environ, {k: v for k, v in env.items() if v is not None},
                clear=False):
            for key, value in env.items():
                if value is None:
                    os.environ.pop(key, None)
            spec.loader.exec_module(module)
    return module


ARCHIVE_DDL = """
CREATE TABLE recordings(
  id TEXT PRIMARY KEY, name TEXT, start_at TEXT, created_at TEXT,
  duration_ms INTEGER, lang TEXT, asr_engine TEXT, asr_transcript TEXT,
  plaud_transcript TEXT, summary TEXT, audio_path TEXT, archived_at TEXT,
  plaud_meta_json TEXT, summary_json TEXT, asr_meta_json TEXT,
  asr_alternative_transcript TEXT);
CREATE VIRTUAL TABLE recordings_fts USING fts5(id UNINDEXED, name, transcript);
"""

# A fixed clock. Every retry/lease assertion reasons from it rather than from
# wall time, so a slow machine cannot make a test flap.
NOW = 1786_100_000


class PipelineTestCase(unittest.TestCase):
    """One archive DB with the pipeline schema applied."""

    def setUp(self):
        self.pipeline = load_module("pipeline.py", f"pipeline_{id(self)}")
        self.dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.dir, "archive.db")
        self.conn = sqlite3.connect(self.db_path)
        self.conn.executescript(ARCHIVE_DDL)
        self.pipeline.ensure_schema(self.conn)
        self.addCleanup(self.conn.close)

    def recording(self, rec_id, **kwargs):
        self.conn.execute(
            """INSERT INTO recordings(id,name,duration_ms,plaud_transcript,
                 asr_transcript,summary,summary_json,audio_path)
               VALUES(?,?,?,?,?,?,?,?)""",
            (rec_id, kwargs.get("name", f"rec {rec_id}"),
             kwargs.get("duration_ms", 14_000),
             kwargs.get("plaud", ""), kwargs.get("asr", ""),
             kwargs.get("summary", ""), kwargs.get("summary_json", ""),
             kwargs.get("audio_path")))
        self.conn.commit()

    def jobs(self):
        return [(row[0], row[1], row[2]) for row in self.conn.execute(
            "SELECT recording_id, stage, state FROM pipeline_jobs ORDER BY seq")]


class EnqueueTests(PipelineTestCase):
    """Enqueue is the durable commitment. Everything else is a hint."""

    def test_enqueue_is_idempotent_for_one_recording_and_stage(self):
        """Duplicate wakeups, a retried ingest pass and a reconciliation sweep
        all enqueue the same work. Two ASR jobs for one recording would mean
        two workers transcribing the same audio and racing each other's
        publication."""
        self.recording("r1")

        first = self.pipeline.enqueue(self.conn, "r1", self.pipeline.STAGE_ASR)
        second = self.pipeline.enqueue(self.conn, "r1", self.pipeline.STAGE_ASR)
        third = self.pipeline.enqueue(self.conn, "r1", self.pipeline.STAGE_ASR)

        self.assertTrue(first)
        self.assertFalse(second)
        self.assertFalse(third)
        self.assertEqual(self.jobs(), [("r1", "asr", "queued")])


class ClaimTests(PipelineTestCase):
    """FIFO by durable enqueue order, one worker per job."""

    def test_jobs_are_claimed_in_enqueue_order_and_only_once(self):
        """The order recordings were committed in is the order they run in —
        not the order a directory listing or a wakeup happened to arrive in.
        A claimed job is invisible to the next claim, so two workers woken by
        the same ingest pass cannot both take it."""
        for rid in ("first", "second", "third"):
            self.recording(rid)
            self.pipeline.enqueue(self.conn, rid, self.pipeline.STAGE_ASR,
                                  now=NOW)

        claimed = self.pipeline.claim_next(self.conn, now=NOW)
        again = self.pipeline.claim_next(self.conn, now=NOW)

        self.assertEqual(claimed["recording_id"], "first")
        self.assertEqual(claimed["stage"], "asr")
        self.assertEqual(again["recording_id"], "second")
        self.assertEqual(
            self.conn.execute(
                "SELECT state FROM pipeline_jobs WHERE recording_id='first'"
            ).fetchone()[0],
            "processing")

    def test_a_job_left_processing_by_a_dead_worker_is_recovered(self):
        """The crash case with no cron behind it. A container killed mid-ASR
        leaves `processing` in the table; nothing else in the system will ever
        notice that recording again unless the next worker to start takes the
        job back. The lease is what bounds how long that takes, and a claim
        whose pid is gone from this host is recovered without waiting for it."""
        self.recording("orphan")
        self.pipeline.enqueue(self.conn, "orphan", self.pipeline.STAGE_ASR,
                              now=NOW)
        self.pipeline.claim_next(self.conn, now=NOW)
        self.conn.execute(
            "UPDATE pipeline_jobs SET claim_owner='ghost-host:424242' "
            "WHERE recording_id='orphan'")
        self.conn.commit()

        self.assertIsNone(self.pipeline.claim_next(self.conn, now=NOW))

        # A restart calls recover_stale before it drains, exactly as the
        # connector does. Inside the lease the claim is still respected.
        self.assertEqual(self.pipeline.recover_stale(self.conn, now=NOW), 0)
        expired = NOW + self.pipeline.LEASE_SEC + 1
        self.assertEqual(self.pipeline.recover_stale(self.conn, now=expired), 1)

        recovered = self.pipeline.claim_next(self.conn, now=expired)
        self.assertEqual(recovered["recording_id"], "orphan")
        self.assertEqual(recovered["attempts"], 0)  # a crash is not a strike

    def test_single_flight_startup_reclaims_a_foreign_container_claim(self):
        self.recording("orphan")
        self.pipeline.enqueue(self.conn, "orphan", self.pipeline.STAGE_ASR,
                              now=NOW)
        self.pipeline.claim_next(self.conn, now=NOW)
        self.conn.execute(
            "UPDATE pipeline_jobs SET claim_owner='retired-container:1' "
            "WHERE recording_id='orphan'")
        self.conn.commit()

        recovered_jobs = self.pipeline.recover_stale_jobs(
            self.conn, now=NOW, reclaim_foreign=True)
        self.assertEqual(
            [(job["recording_id"], job["stage"]) for job in recovered_jobs],
            [("orphan", self.pipeline.STAGE_ASR)],
        )
        recovered = self.pipeline.claim_next(self.conn, now=NOW)
        self.assertEqual(recovered["recording_id"], "orphan")
        self.assertEqual(recovered["attempts"], 0)


class StageChainTests(PipelineTestCase):
    """Finishing one stage is what enqueues the next. No clock in between."""

    def claim(self, rid, stage):
        job = self.pipeline.claim_next(self.conn, now=NOW, stages=(stage,))
        self.assertIsNotNone(job)
        self.assertEqual(job["recording_id"], rid)
        return job

    def test_completing_a_stage_enqueues_the_next_one_in_the_same_commit(self):
        """The gap this closes is the whole point of the rewrite: a transcript
        used to be published and the summary then waited for a cron tick. Both
        the completion and the successor must land in ONE transaction, or a
        crash between them leaves a transcript that nothing will ever summarise
        and no schedule to notice."""
        self.recording("r1")
        self.pipeline.enqueue(self.conn, "r1", self.pipeline.STAGE_ASR, now=NOW)
        job = self.claim("r1", self.pipeline.STAGE_ASR)

        self.pipeline.complete(self.conn, job, now=NOW)

        self.assertEqual(self.jobs(),
                         [("r1", "asr", "done"), ("r1", "summary", "queued")])
        # And it keeps going, one stage at a time, all the way to ready.
        self.pipeline.complete(
            self.conn, self.claim("r1", self.pipeline.STAGE_SUMMARY), now=NOW)
        self.pipeline.complete(
            self.conn, self.claim("r1", self.pipeline.STAGE_MINDMAP), now=NOW)
        self.pipeline.complete(
            self.conn, self.claim("r1", self.pipeline.STAGE_CARD), now=NOW)

        self.assertEqual([stage for _rid, stage, _state in self.jobs()],
                         ["asr", "summary", "mindmap", "card"])
        self.assertEqual({state for _rid, _stage, state in self.jobs()}, {"done"})

    def test_a_failed_successor_write_leaves_the_stage_unfinished(self):
        """All or nothing. A stage marked done whose successor was never
        written is a recording that stops silently halfway down the chain, and
        nothing in the system re-derives that from the transcript alone."""
        self.recording("r1")
        self.pipeline.enqueue(self.conn, "r1", self.pipeline.STAGE_ASR, now=NOW)
        job = self.claim("r1", self.pipeline.STAGE_ASR)

        broken = FailingConn(self.conn, on="INSERT INTO pipeline_jobs")
        with self.assertRaises(sqlite3.OperationalError):
            self.pipeline.complete(broken, job, now=NOW)

        self.assertEqual(self.jobs(), [("r1", "asr", "processing")])


class FailureTests(PipelineTestCase):
    """Each stage retries on its own budget."""

    def test_a_summary_failure_never_reruns_asr(self):
        """Stage independence, pinned. Re-running ASR because a summary call
        timed out would spend GPU hours on audio that is already transcribed,
        and — worse — re-publish a transcript the review already selected."""
        self.recording("r1", asr="готовый текст")
        self.pipeline.enqueue(self.conn, "r1", self.pipeline.STAGE_ASR, now=NOW)
        asr_job = self.pipeline.claim_next(self.conn, now=NOW)
        self.pipeline.complete(self.conn, asr_job, now=NOW)
        summary_job = self.pipeline.claim_next(self.conn, now=NOW)

        self.pipeline.fail(self.conn, summary_job,
                           RuntimeError("summary tool exploded"), now=NOW)

        self.assertEqual(self.jobs(),
                         [("r1", "asr", "done"), ("r1", "summary", "retry_wait")])
        # The retry is scheduled, not immediate: hammering a service that just
        # refused is how a transient becomes an outage.
        row = self.conn.execute(
            "SELECT attempts, available_epoch FROM pipeline_jobs "
            "WHERE stage='summary'").fetchone()
        self.assertEqual(row[0], 1)
        self.assertGreater(row[1], NOW)
        self.assertIsNone(self.pipeline.claim_next(self.conn, now=NOW))
        self.assertIsNotNone(
            self.pipeline.claim_next(self.conn, now=row[1]))

    def test_a_stage_that_exhausts_its_budget_fails_and_stops(self):
        self.recording("r1")
        self.pipeline.enqueue(self.conn, "r1", self.pipeline.STAGE_ASR, now=NOW)
        clock = NOW
        for _ in range(self.pipeline.MAX_ATTEMPTS):
            job = self.pipeline.claim_next(self.conn, now=clock)
            self.assertIsNotNone(job)
            self.pipeline.fail(self.conn, job, ValueError("decode failed"),
                               now=clock)
            clock = self.conn.execute(
                "SELECT available_epoch FROM pipeline_jobs WHERE seq=?",
                (job["seq"],)).fetchone()[0]

        self.assertEqual(self.jobs(), [("r1", "asr", "failed")])
        self.assertIsNone(self.pipeline.claim_next(self.conn, now=clock + 10**6))

    def test_a_transient_failure_costs_no_budget(self):
        """An asr-mcp restart says nothing about the audio. Three unlucky
        redeploys must not retire a recording nothing is wrong with."""
        self.recording("r1")
        self.pipeline.enqueue(self.conn, "r1", self.pipeline.STAGE_ASR, now=NOW)
        clock = NOW
        for _ in range(self.pipeline.MAX_ATTEMPTS + 3):
            job = self.pipeline.claim_next(self.conn, now=clock)
            self.assertIsNotNone(job)
            self.pipeline.fail(
                self.conn, job,
                RuntimeError("HTTP Error 503: Service Unavailable"), now=clock)
            clock = self.conn.execute(
                "SELECT available_epoch FROM pipeline_jobs WHERE seq=?",
                (job["seq"],)).fetchone()[0]

        self.assertEqual(self.jobs(), [("r1", "asr", "retry_wait")])
        self.assertEqual(
            self.conn.execute(
                "SELECT attempts FROM pipeline_jobs").fetchone()[0], 0)


class ClaimFencingTests(PipelineTestCase):
    """Two real connections, one job. Only the current owner may write.

    A lease expiring does not prove the old worker is gone — it proves we
    stopped believing it. An ASR call can trickle bytes for hours past any
    inactivity timeout, so the previous owner may still be alive, still
    transcribing, and about to report success for a job that now belongs to
    somebody else. Every mutation is therefore fenced on the claim it was
    made under.
    """

    def setUp(self):
        super().setUp()
        self.other = sqlite3.connect(self.db_path)
        self.addCleanup(self.other.close)
        self.recording("gpu")
        self.pipeline.enqueue(self.conn, "gpu", self.pipeline.STAGE_ASR,
                              now=NOW)

    def reissue(self):
        """Worker 1 claims, its lease expires, worker 2 takes the job."""
        first = self.pipeline.claim_next(self.conn, now=NOW)
        # A claim from a machine that is not this host: only the lease can
        # speak for it, which is exactly the ambiguous case.
        self.conn.execute(
            "UPDATE pipeline_jobs SET claim_owner='far-away-host:7' "
            "WHERE seq=?", (first["seq"],))
        self.conn.commit()
        first["claim_owner"] = "far-away-host:7"
        later = NOW + self.pipeline.LEASE_SEC + 1
        self.pipeline.recover_stale(self.other, now=later)
        second = self.pipeline.claim_next(self.other, now=later)
        self.assertIsNotNone(second)
        self.assertEqual(second["seq"], first["seq"])
        return first, second, later

    def state(self):
        return self.conn.execute(
            "SELECT state FROM pipeline_jobs WHERE recording_id='gpu'"
        ).fetchone()[0]

    def test_a_stale_owner_cannot_complete_a_reissued_job(self):
        """The worst outcome in the system: worker 1 marks done a job worker 2
        is still transcribing, so the chain advances on a transcript that was
        never published and the summary runs against nothing."""
        stale, _current, later = self.reissue()

        self.assertFalse(self.pipeline.complete(self.conn, stale, now=later))

        self.assertEqual(self.state(), "processing")
        self.assertEqual(self.jobs(), [("gpu", "asr", "processing")])

    def test_a_stale_owner_cannot_fail_a_reissued_job(self):
        """Otherwise a dying worker spends the retry budget of the worker that
        replaced it, and three slow redeploys retire the recording."""
        stale, _current, later = self.reissue()

        self.assertFalse(
            self.pipeline.fail(self.conn, stale, ValueError("decode failed"),
                               now=later))

        self.assertEqual(self.state(), "processing")
        self.assertEqual(
            self.conn.execute(
                "SELECT attempts FROM pipeline_jobs").fetchone()[0], 0)

    def test_a_stale_owner_cannot_defer_a_reissued_job(self):
        stale, _current, later = self.reissue()

        self.assertFalse(
            self.pipeline.defer(self.conn, stale,
                                self.pipeline.StageDeferred("1/9 owed"),
                                now=later))

        self.assertEqual(self.state(), "processing")

    def test_a_stale_owner_cannot_extend_a_lease_it_no_longer_holds(self):
        """A heartbeat from the old owner would keep renewing the claim under
        worker 2 and could hold the job in `processing` forever."""
        stale, current, later = self.reissue()
        before = self.conn.execute(
            "SELECT claim_owner, claim_epoch FROM pipeline_jobs").fetchone()

        self.assertFalse(self.pipeline.heartbeat(self.conn, stale,
                                                 now=later + 10))

        self.assertEqual(
            self.conn.execute(
                "SELECT claim_owner, claim_epoch FROM pipeline_jobs").fetchone(),
            before)
        self.assertEqual(before[0], current["claim_owner"])

    def test_the_current_owner_still_works_normally(self):
        """Fencing must not cost the legitimate worker anything."""
        _stale, current, later = self.reissue()

        self.assertTrue(self.pipeline.heartbeat(self.other, current, now=later))
        self.assertTrue(self.pipeline.complete(self.other, current, now=later))

        self.assertEqual(self.jobs(),
                         [("gpu", "asr", "done"), ("gpu", "summary", "queued")])

    def test_a_worker_that_lost_its_claim_is_told_so_it_can_stop(self):
        """A long ASR job checkpoints between windows. If the heartbeat says
        the claim is gone, continuing means two GPUs on the same audio — so the
        worker has to learn about it at the checkpoint, not at the end."""
        stale, _current, later = self.reissue()

        with self.assertRaises(self.pipeline.ClaimLost):
            self.pipeline.checkpoint(self.conn, stale, now=later + 5)

    def test_a_drain_that_loses_a_claim_leaves_the_job_to_its_new_owner(self):
        """The losing worker must not record anything at all — not a failure,
        not a strike, not a completion."""
        self.pipeline.recover_stale(self.conn, now=NOW)
        ran = []

        def steal_then_finish(conn, job):
            ran.append(job["recording_id"])
            # Somebody else takes the job mid-handler, as an expired lease
            # followed by another worker's claim would.
            self.other.execute(
                "UPDATE pipeline_jobs SET claim_owner='other:1', claim_epoch=? "
                "WHERE seq=?", (NOW + 5, job["seq"]))
            self.other.commit()

        self.pipeline.drain(
            self.conn, {self.pipeline.STAGE_ASR: steal_then_finish}, now=NOW)

        self.assertEqual(ran, ["gpu"])
        self.assertEqual(self.jobs(), [("gpu", "asr", "processing")])
        self.assertEqual(
            self.conn.execute(
                "SELECT attempts, claim_owner FROM pipeline_jobs").fetchone(),
            (0, "other:1"))


class DrainTests(PipelineTestCase):
    """The worker. A wake tells it to look; the table tells it what to do."""

    def setUp(self):
        super().setUp()
        self.ran = []

    def handlers(self, **overrides):
        def record(stage):
            def handler(conn, job):
                self.ran.append((job["recording_id"], stage))
            return handler

        built = {stage: record(stage) for stage in self.pipeline.STAGES}
        built.update(overrides)
        return built

    def test_one_wake_carries_a_recording_all_the_way_to_ready(self):
        """No clock anywhere in this test. Enqueueing ASR and waking the worker
        once is enough to reach the end of the chain, because each stage
        commits its successor as it finishes."""
        self.recording("r1")
        self.pipeline.enqueue(self.conn, "r1", self.pipeline.STAGE_ASR, now=NOW)

        self.pipeline.drain(self.conn, self.handlers(), now=NOW)

        self.assertEqual(self.ran, [("r1", "asr"), ("r1", "summary"),
                                    ("r1", "mindmap"), ("r1", "card")])
        self.assertEqual({state for _r, _s, state in self.jobs()}, {"done"})
        self.assertTrue(self.pipeline.is_ready(self.conn, "r1"))

    def test_duplicate_wakeups_do_not_duplicate_work(self):
        """Ingest wakes the worker, reconciliation wakes it, an internal signal
        wakes it. Extra wakes must cost a query, not a second transcription."""
        self.recording("r1")
        self.pipeline.enqueue(self.conn, "r1", self.pipeline.STAGE_ASR, now=NOW)

        for _ in range(4):
            self.pipeline.drain(self.conn, self.handlers(), now=NOW)

        self.assertEqual([stage for _rid, stage in self.ran],
                         ["asr", "summary", "mindmap", "card"])

    def test_a_failing_stage_stops_that_recording_and_not_the_queue(self):
        """A PLAUD outage, a bad summary, one unreadable file: whatever it is,
        the other recordings behind it must still drain."""
        for rid in ("bad", "good"):
            self.recording(rid)
            self.pipeline.enqueue(self.conn, rid, self.pipeline.STAGE_ASR,
                                  now=NOW)

        def explode(conn, job):
            if job["recording_id"] == "bad":
                raise ValueError("decode failed")
            self.ran.append((job["recording_id"], job["stage"]))

        self.pipeline.drain(self.conn, self.handlers(asr=explode), now=NOW)

        self.assertEqual(
            [(rid, stage) for rid, stage in self.ran if rid == "good"],
            [("good", "asr"), ("good", "summary"), ("good", "mindmap"),
             ("good", "card")])
        self.assertIn(("bad", "asr", "retry_wait"), self.jobs())

    def test_a_deferred_stage_keeps_its_place_without_a_strike(self):
        """A long recording checkpoints a window and hands the queue back. That
        is not a failure — a nine-window recording must not accumulate nine
        strikes just for taking nine turns — and the job stays owed."""
        self.recording("long", duration_ms=4 * 3600 * 1000)
        self.pipeline.enqueue(self.conn, "long", self.pipeline.STAGE_ASR,
                              now=NOW)

        def paused(conn, job):
            self.ran.append((job["recording_id"], "window"))
            raise self.pipeline.StageDeferred("2/9 segments incomplete")

        self.pipeline.drain(self.conn, self.handlers(asr=paused), now=NOW)

        self.assertEqual(self.ran, [("long", "window")])
        self.assertEqual(self.jobs(), [("long", "asr", "queued")])
        self.assertEqual(
            self.conn.execute(
                "SELECT attempts FROM pipeline_jobs").fetchone()[0], 0)

    def test_a_drain_is_bounded_so_one_worker_cannot_be_held_forever(self):
        """The same pass also has to get back to polling PLAUD."""
        for index in range(5):
            self.recording(f"r{index}")
            self.pipeline.enqueue(self.conn, f"r{index}",
                                  self.pipeline.STAGE_ASR, now=NOW)

        self.pipeline.drain(self.conn, self.handlers(), now=NOW, max_jobs=3)

        self.assertEqual(len(self.ran), 3)

    def test_a_stage_with_no_handler_is_recorded_not_silently_skipped(self):
        """A deployment missing a stage must be visible as a failed job, not as
        a recording that quietly stops halfway and reads as ready."""
        self.recording("r1")
        self.pipeline.enqueue(self.conn, "r1", self.pipeline.STAGE_ASR, now=NOW)

        self.pipeline.drain(self.conn, {}, now=NOW)

        self.assertEqual(self.jobs(), [("r1", "asr", "retry_wait")])
        self.assertFalse(self.pipeline.is_ready(self.conn, "r1"))


class WakeTests(PipelineTestCase):
    """A wakeup is a hint, and hints are allowed to be lost or repeated."""

    def test_repeated_wakes_collapse_into_one_pending_signal(self):
        waker = self.pipeline.Waker()
        for _ in range(5):
            waker.wake()

        self.assertTrue(waker.wait(timeout=0))
        self.assertFalse(waker.wait(timeout=0),
                         "a consumed signal must not fire twice")

    def test_losing_every_wakeup_loses_no_work(self):
        """The property the whole design rests on. Nothing calls wake here, and
        the job still runs the moment a worker looks at the table."""
        self.recording("r1")
        self.pipeline.enqueue(self.conn, "r1", self.pipeline.STAGE_ASR, now=NOW)
        ran = []

        self.pipeline.drain(
            self.conn,
            {stage: lambda conn, job: ran.append(job["stage"])
             for stage in self.pipeline.STAGES},
            now=NOW)

        self.assertEqual(ran, ["asr", "summary", "mindmap", "card"])


class ReconcileTests(PipelineTestCase):
    """The repair pass. It is what makes the 5-minute poll a reconciliation
    rather than a discovery-only fetch."""

    def setUp(self):
        super().setUp()
        self.audio_dir = os.path.join(self.dir, "audio")
        os.makedirs(self.audio_dir)

    def audio(self, rec_id, payload=b"ID3fake"):
        path = os.path.join(self.audio_dir, f"{rec_id}.mp3")
        Path(path).write_bytes(payload)
        return path

    def test_an_audio_present_row_with_no_job_is_repaired(self):
        """The interrupted-enqueue case. Audio is on disk and the row exists,
        but nothing is queued — an older code path wrote it, or a process died
        between the two. Without this the recording is invisible work forever:
        no cron looks at it, because there is no cron."""
        self.recording("stranded")
        self.audio("stranded")

        repaired = self.pipeline.reconcile(
            self.conn, audio_dir=self.audio_dir, now=NOW)

        self.assertEqual(repaired, ["stranded"])
        self.assertEqual(self.jobs(), [("stranded", "asr", "queued")])

    def test_available_diarization_engine_adopts_audio_missed_while_models_were_absent(self):
        """Model provisioning may trail connector startup during a rolling release.

        Audio archived while the optional engine was unavailable must become
        eligible on a later reconciliation pass, without re-ingest or an operator
        request, once the immutable local models appear.
        """
        self.recording("late-model")
        self.audio("late-model")

        self.pipeline.reconcile(
            self.conn, audio_dir=self.audio_dir, now=NOW,
            diarization_available=False)
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM pipeline_jobs WHERE stage='diarization'"
            ).fetchone()[0], 0)

        adopted = self.pipeline.reconcile(
            self.conn, audio_dir=self.audio_dir, now=NOW + 1,
            diarization_available=True)

        self.assertIn("late-model", adopted)
        self.assertEqual(
            self.conn.execute(
                "SELECT state FROM pipeline_jobs WHERE recording_id=? AND stage=?",
                ("late-model", self.pipeline.STAGE_DIARIZATION),
            ).fetchone()[0], self.pipeline.JOB_QUEUED)

    def test_reconcile_is_idempotent_and_never_duplicates_live_work(self):
        """It runs every poll, forever. A second job for the same recording
        would put two workers on the same audio."""
        self.recording("stranded")
        self.audio("stranded")
        self.pipeline.reconcile(self.conn, audio_dir=self.audio_dir, now=NOW)
        self.pipeline.claim_next(self.conn, now=NOW)   # in flight right now

        self.assertEqual(
            self.pipeline.reconcile(self.conn, audio_dir=self.audio_dir, now=NOW),
            [])
        self.assertEqual(self.jobs(), [("stranded", "asr", "processing")])

    def test_a_row_with_no_local_audio_is_not_queued_for_local_asr(self):
        """Queueing it would hand the worker a job whose only honest outcome is
        'no audio', spending its whole retry budget to discover that."""
        self.recording("audioless")

        self.assertEqual(
            self.pipeline.reconcile(self.conn, audio_dir=self.audio_dir, now=NOW),
            [])
        self.assertEqual(self.jobs(), [])

    def test_a_finished_recording_is_not_dragged_back_through_asr(self):
        """A row whose ASR stage already ran — the job is done — must not be
        re-queued by a repair pass, or every poll would re-transcribe the whole
        archive."""
        self.recording("finished", asr="готовый текст")
        self.audio("finished")
        self.pipeline.enqueue(self.conn, "finished", self.pipeline.STAGE_ASR,
                              now=NOW)
        job = self.pipeline.claim_next(self.conn, now=NOW)
        self.pipeline.complete(self.conn, job, now=NOW)

        self.assertEqual(
            self.pipeline.reconcile(self.conn, audio_dir=self.audio_dir, now=NOW),
            [])

    def test_repair_is_bounded_per_pass_and_says_what_it_deferred(self):
        """Importing years of history must not put the whole archive in the
        queue in one pass. What is deferred is logged rather than dropped: the
        next poll is five minutes away, and a silent cap reads as 'covered
        everything' when it did not."""
        for index in range(7):
            self.recording(f"old{index}")
            self.audio(f"old{index}")
        deferred = []

        with mock.patch.object(self.pipeline, "RECONCILE_LIMIT", 3), \
                mock.patch.object(self.pipeline, "log", deferred.append):
            first = self.pipeline.reconcile(
                self.conn, audio_dir=self.audio_dir, now=NOW)

        self.assertEqual(len(first), 3)
        self.assertTrue(any("deferred" in line for line in deferred), deferred)
        with mock.patch.object(self.pipeline, "RECONCILE_LIMIT", 3):
            self.pipeline.reconcile(self.conn, audio_dir=self.audio_dir, now=NOW)
            self.pipeline.reconcile(self.conn, audio_dir=self.audio_dir, now=NOW)
        self.assertEqual(len(self.jobs()), 7)

    def test_reconcile_also_recovers_jobs_abandoned_by_a_dead_worker(self):
        """One repair entry point, not two: the poll that repairs a missing
        enqueue is the same pass that must un-stick a job whose worker died."""
        self.recording("stuck")
        self.audio("stuck")
        self.pipeline.reconcile(self.conn, audio_dir=self.audio_dir, now=NOW)
        self.pipeline.claim_next(self.conn, now=NOW)

        self.pipeline.reconcile(self.conn, audio_dir=self.audio_dir,
                                now=NOW + self.pipeline.LEASE_SEC + 1)

        self.assertEqual(self.jobs(), [("stuck", "asr", "queued")])


class FailingConn:
    """A connection whose first matching statement fails, as a disk error would.

    Everything else passes straight through, so the rollback under test is the
    real one on the real connection.
    """

    def __init__(self, conn, on):
        self._conn = conn
        self._on = on

    def execute(self, sql, *args):
        if self._on in sql:
            raise sqlite3.OperationalError("disk I/O error")
        return self._conn.execute(sql, *args)

    def __getattr__(self, name):
        return getattr(self._conn, name)


if __name__ == "__main__":
    unittest.main()
