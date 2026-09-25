import os
import json
import logging
from datetime import datetime
from pydantic import BaseModel
from typing import List, Optional
#: Blob prefix for generated datasets. Whole-file write, whole-file read,
#: listing — nothing here needs a directory, so this needs no `runtime_dirs`
#: entry. Generation streams to scratch and publishes at the end, the same
#: shape as run logs, so a crashed job still leaves its partial output behind
#: on the machine that was running it.
DATASETS_PREFIX = "datasets"

logger = logging.getLogger(__name__)

class SyntheticDataRequest(BaseModel):
    """One generation job.

    There is no provider and no key here (F210): records come from the engine's
    own model call, so the model is any the workspace can reach and its key
    stays on the server. It used to take a key from the browser and put it in a
    URL — which every failed record then printed into the log.
    """
    topic: str
    count: int
    model: Optional[str] = None  # empty = the workspace's default model
    system_prompt: Optional[str] = "You are a helpful assistant."
    edge_cases: Optional[str] = ""
    examples: Optional[List[dict]] = [] # [{"user": "...", "assistant": "..."}]

class GenerationStatus(BaseModel):
    status: str # "idle", "generating", "completed", "failed"
    completed_count: int
    total_count: int
    current_file: Optional[str] = None
    error: Optional[str] = None

# Global Status Tracker (Simple in-memory for POC)
current_job = {
    "status": "idle",
    "completed": 0,
    "total": 0,
    "file": None,
    "error": None
}

async def generate_synthetic_data(request: SyntheticDataRequest):
    global current_job
    current_job["status"] = "generating"
    current_job["total"] = request.count
    current_job["completed"] = 0
    current_job["error"] = None
    
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"dataset_{request.topic.replace(' ', '_')}_{timestamp}.jsonl"
    from core.storage.scratch import scratch_dir
    filepath = os.path.join(str(scratch_dir("datasets")), filename)
    current_job["file"] = filename

    # Meta Prompt Construction
    meta_system_prompt = f"""
    You are an expert data generator for training LLMs.
    Your task is to generate diverse, high-quality conversation pairs (User query and Assistant response) for the topic: '{request.topic}'.
    
    Target Persona for Assistant:
    {request.system_prompt}
    
    Edge Cases and Constraints to handle:
    {request.edge_cases}
    
    Generate PRECISELY ONE conversation pair in JSON format:
    {{
        "messages": [
            {{"role": "user", "content": "..."}},
            {{"role": "assistant", "content": "..."}}
        ]
    }}
    Do not output markdown code blocks. Output ONLY the raw JSON object.
    """
    
    if request.examples:
        meta_system_prompt += "\n\nFollow this style from these examples:\n"
        for ex in request.examples:
            meta_system_prompt += f"User: {ex['user']}\nAssistant: {ex['assistant']}\n---\n"

    from core.config import load_settings
    from core.llm_providers import detect_mode_from_model, generate_response

    settings = load_settings()
    model = request.model or settings.get("model", "")
    mode = detect_mode_from_model(model)

    try:
        failed, first_error = 0, None
        for i in range(request.count):
            try:
                content = await generate_response(
                    prompt_msg=f"Generate conversation #{i+1}.",
                    sys_prompt=meta_system_prompt,
                    mode=mode,
                    current_model=model,
                    current_settings=settings,
                )

                # Cleanup JSON
                json_str = content.strip()
                if json_str.startswith("```json"):
                    json_str = json_str[7:-3]
                elif json_str.startswith("```"):
                    json_str = json_str[3:-3]

                record = json.loads(json_str)

                # Append to file immediately (streaming save)
                with open(filepath, "a", encoding="utf-8") as f:
                    f.write(json.dumps(record) + "\n")

                current_job["completed"] += 1

            except Exception as e:
                # Continue to the next record; the job says how many failed.
                failed += 1
                first_error = first_error or f"{type(e).__name__}: {e}"
                logger.warning("Data Lab: record %d of %d failed: %s", i + 1, request.count, e)

        if current_job["completed"] == 0:
            # Nothing was written, so there is nothing to publish. This used to
            # end "completed", with 0 records and no error.
            current_job["status"] = "failed"
            current_job["error"] = f"No records were generated. {first_error or ''}".strip()
            return

        # Publish the finished dataset. In a local install the blob store is a
        # directory the user can open; in a hosted one it is per-org S3.
        try:
            from core.storage import get_blob_store
            with open(filepath, "r", encoding="utf-8") as f:
                get_blob_store().put(f"{DATASETS_PREFIX}/{filename}", f.read())
        except Exception as e:
            logger.warning("Data Lab: could not publish dataset %s: %s", filename, e)

        current_job["status"] = "completed"
        if failed:
            current_job["error"] = f"{failed} of {request.count} failed: {first_error}"

    except Exception as e:
        current_job["status"] = "failed"
        current_job["error"] = str(e)
        logger.warning("Data Lab: job failed: %s", e)
