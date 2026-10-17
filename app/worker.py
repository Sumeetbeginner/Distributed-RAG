"""At-least-once indexing with renewable leases and token-fenced publication."""

import logging
import random
import signal
import threading
import time
from dataclasses import dataclass
from sqlalchemy import and_, or_, select, update
from app.config import get_settings
from app.db import Document, Job, uid
from app.retrieval import chunk_text
from app.runtime import Runtime
from app.services import lock_tenant

logger = logging.getLogger("distributedrag.worker")


@dataclass(frozen=True)
class Claim:
    id: str
    tenant_id: str
    document_id: str
    revision: int
    operation: str
    token: str


def claim_job(runtime):
    while True:
        now = time.time()
        exhausted = None
        with runtime.session.begin() as session:
            job = session.scalar(
                select(Job)
                .where(
                    or_(
                        and_(Job.state == "queued", Job.available_at <= now),
                        and_(Job.state == "running", Job.lease_until < now),
                    )
                )
                .order_by(Job.available_at, Job.id)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if not job:
                return None
            if job.attempts >= runtime.settings.max_attempts:
                job.state, job.error = "dead", "Attempt limit reached after lease expiry"
                exhausted = (job.tenant_id, job.document_id, job.revision, job.id)
            else:
                job.state, job.token = "running", uid()
                job.attempts += 1
                job.lease_until = now + runtime.settings.lease_seconds
                return Claim(job.id, job.tenant_id, job.document_id, job.revision, job.operation, job.token)
        # Release the job lock before taking tenant/document locks (consistent lock ordering).
        if exhausted:
            tenant_id, doc_id, revision, job_id = exhausted
            with runtime.session.begin() as session:
                lock_tenant(session, tenant_id)
                doc = session.get(Document, doc_id)
                job = session.get(Job, job_id)
                if doc.revision == revision and not doc.deleted and job.state == "dead":
                    doc.status = "failed"


def owned(job, claim):
    return job and job.state == "running" and job.token == claim.token and job.lease_until > time.time()


def renew(runtime, claim):
    with runtime.session.begin() as session:
        result = session.execute(
            update(Job)
            .where(
                Job.id == claim.id,
                Job.token == claim.token,
                Job.state == "running",
                Job.lease_until > time.time(),
            )
            .values(lease_until=time.time() + runtime.settings.lease_seconds)
        )
        return result.rowcount == 1


def publish(runtime, claim, chunk_count):
    with runtime.session.begin() as session:
        tenant = lock_tenant(session, claim.tenant_id)
        doc = session.scalar(select(Document).where(Document.id == claim.document_id).with_for_update())
        job = session.scalar(select(Job).where(Job.id == claim.id).with_for_update())
        if not owned(job, claim):
            return False
        if doc.revision != claim.revision or (doc.deleted and claim.operation != "delete"):
            job.state = "superseded"
            return False
        if claim.operation == "index":
            doc.active_revision, doc.chunk_count, doc.status = claim.revision, chunk_count, "indexed"
        job.state, job.error = "done", ""
        tenant.generation += 1
        return True


def fail(runtime, claim, error):
    with runtime.session.begin() as session:
        lock_tenant(session, claim.tenant_id)
        doc = session.scalar(select(Document).where(Document.id == claim.document_id).with_for_update())
        job = session.scalar(select(Job).where(Job.id == claim.id).with_for_update())
        if not owned(job, claim):
            return
        if doc.revision != claim.revision:
            job.state = "superseded"
            return
        # Exception class only: provider responses can contain sensitive document text or URLs.
        job.error = type(error).__name__ + ": indexing failed; inspect protected worker logs"
        job.state = "dead" if job.attempts >= runtime.settings.max_attempts else "queued"
        job.available_at = time.time() + min(300, 2**job.attempts) + random.random()
        if not doc.deleted:
            doc.status = "failed" if job.state == "dead" else "retrying"


def process(runtime, claim):
    with runtime.session() as session:
        doc = session.get(Document, claim.document_id)
        if doc.revision != claim.revision or (doc.deleted and claim.operation != "delete"):
            publish_needed = True
            data = None
        else:
            publish_needed = False
            data = {
                key: getattr(doc, key)
                for key in ("id", "tenant_id", "revision", "title", "content", "source", "collection")
            }
    if publish_needed:
        publish(runtime, claim, 0)
        return
    if claim.operation == "delete":
        runtime.index.cleanup(claim.tenant_id, claim.document_id, claim.revision)
        publish(runtime, claim, 0)
        return
    parts = chunk_text(data["content"], runtime.settings.chunk_words, runtime.settings.chunk_overlap)
    vectors = runtime.embeddings.embed(parts)
    rows = [
        {
            "id": f"{claim.document_id}:{claim.revision}:{ordinal}",
            "document_id": claim.document_id,
            "tenant_id": claim.tenant_id,
            "revision": claim.revision,
            "ordinal": ordinal,
            "title": data["title"],
            "content": part,
            "source": data["source"],
            "collection": data["collection"],
            "vector": vector,
        }
        for ordinal, (part, vector) in enumerate(zip(parts, vectors))
    ]
    runtime.index.put(rows)
    if publish(runtime, claim, len(rows)):
        try:
            runtime.index.cleanup(claim.tenant_id, claim.document_id, claim.revision)
        except Exception:
            # Publication succeeded; retention cleanup is safe to retry via the gc command.
            logger.warning("retention cleanup deferred document=%s", claim.document_id)


def run_once(runtime, heartbeat=True):
    claim = claim_job(runtime)
    if not claim:
        return False
    stopped = threading.Event()

    def pulse():
        while not stopped.wait(runtime.settings.lease_seconds / 3):
            try:
                if not renew(runtime, claim):
                    return
            except Exception:
                logger.warning("lease renewal failed job=%s", claim.id)
                return

    thread = threading.Thread(target=pulse, daemon=True) if heartbeat else None
    if thread:
        thread.start()
    try:
        process(runtime, claim)
        logger.info("processed job=%s revision=%s", claim.id, claim.revision)
    except Exception as error:
        logger.warning("indexing failure job=%s type=%s", claim.id, type(error).__name__)
        fail(runtime, claim, error)
    finally:
        stopped.set()
        if thread:
            thread.join(timeout=2)
    return True


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    runtime = Runtime(get_settings())
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    try:
        while not stop.is_set():
            try:
                if not run_once(runtime):
                    stop.wait(1)
            except Exception as error:
                logger.error("worker dependency unavailable type=%s", type(error).__name__)
                stop.wait(2)
    finally:
        runtime.close()


if __name__ == "__main__":
    main()
