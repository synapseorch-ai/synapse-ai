import chromadb
from typing import Any, Callable, Optional
import uuid
import os
import json
from datetime import datetime

class MemoryStore:
    def __init__(
        self,
        storage_path="chroma_db",
        model="llama3",
        embed_fn: Optional[Callable[[str], list[float] | None]] = None,
    ):
        # ChromaDB's index is a rebuildable per-replica cache, not tenant
        # state: the embeddings can be regenerated from their sources, and D10
        # keeps vectors out of the store because SQLite has no vector type. So
        # it lives in the state dir. `storage_path` has been ignored since this
        # was written; it stays in the signature so the existing construction
        # sites do not have to change.
        from core.runtime_dirs import state_dir

        self.storage_path = str(state_dir("chroma_db"))

        # Chroma's own usage telemetry off, explicitly: the locked release's is a
        # no-op, but `chromadb>=0.5` is allowed and older ones sent PostHog
        # events. Nothing leaves the machine unless an operator sends it.
        from chromadb.config import Settings as _ChromaSettings
        self.client = chromadb.PersistentClient(
            path=self.storage_path, settings=_ChromaSettings(anonymized_telemetry=False))
        self.collection = self.client.get_or_create_collection(name="chat_history")
        self.model = model  # unused since F327; kept for the construction sites
        self._embed_fn = embed_fn
        print(f"DEBUG: MemoryStore initialized at {self.storage_path}")

    def get_embedding(self, text):
        # CRITICAL: AWS Bedrock embedding models have TWO limits:
        # 1. Character limit: 50,000 chars
        # 2. Token limit: 8,192 tokens (this is the real constraint!)
        # 
        # Ratio: ~3 chars per token
        # To stay under 8,192 tokens, limit to ~20,000 chars (~6,600 tokens with safety margin)
        MAX_CHARS = 20000
        if len(text) > MAX_CHARS:
            original_len = len(text)
            text = text[:MAX_CHARS]
            print(f"WARNING: Truncated embedding text from {original_len} to {MAX_CHARS} chars to stay within token limit")
        
        # No embedder of its own (F327): the step semantic cache embeds with
        # Settings → Models' Embedding Model itself, and nothing else asks.
        if not self._embed_fn:
            return None
        try:
            return self._embed_fn(text)
        except Exception as e:
            print(f"Error getting embedding from configured provider: {e}")
            return None

    def add_memory(self, role, content, metadata: dict[str, Any] | None = None):
        if not content or not content.strip():
            return
            
        embedding = self.get_embedding(content)
        if not embedding:
            return

        doc_id = str(uuid.uuid4())
        base_meta: dict[str, Any] = {
            "role": role,
            "timestamp": str(os.path.getmtime(__file__)),  # Dummy timestamp for now
        }
        if metadata and isinstance(metadata, dict):
            base_meta.update(metadata)

        self.collection.add(
            ids=[doc_id],
            embeddings=[embedding],
            documents=[content],
            metadatas=[base_meta]
        )
        print(f"DEBUG: Added memory to DB: {role}: {content[:30]}...")

    def query_memory(self, query, n_results=5, where: dict[str, Any] | None = None):
        embedding = self.get_embedding(query)
        if not embedding:
            return []

        try:
            query_kwargs: dict[str, Any] = {
                "query_embeddings": [embedding],
                "n_results": n_results,
            }
            if where and isinstance(where, dict):
                query_kwargs["where"] = where

            results = self.collection.query(**query_kwargs)
            
            # Format results
            memories = []
            if results['documents']:
                for i, doc in enumerate(results['documents'][0]):
                    role = results['metadatas'][0][i].get('role', 'unknown')
                    memories.append(f"{role}: {doc}")
            
            return memories
        except Exception as e:
            print(f"Error querying memory: {e}")
            return []

    def add_tool_execution(self, session_id: str, tool_name: str, 
                           tool_args: dict, tool_output: str, 
                           timestamp: str = None, agent_id: str = None):
        """Store tool execution details for session-scoped retrieval.
        
        ID-AGNOSTIC: Automatically extracts any field ending with '_id' or 'Id'
        from the tool output for easy retrieval.
        """
        # Create searchable text representation — truncate large outputs to avoid
        # exceeding embedding model context limits (Ollama models ~2K-8K tokens)
        MAX_OUTPUT_CHARS = 2000
        truncated_output = tool_output[:MAX_OUTPUT_CHARS] if len(tool_output) > MAX_OUTPUT_CHARS else tool_output
        content = f"Tool: {tool_name}\nArguments: {json.dumps(tool_args)}\nOutput: {truncated_output}"
        
        # Store with rich metadata
        metadata = {
            "type": "tool_execution",
            "session_id": session_id,
            "tool_name": tool_name,
            "timestamp": timestamp or datetime.now().isoformat()
        }
        if agent_id:
            metadata["agent_id"] = agent_id
        
        # Add parsed IDs as metadata for easy retrieval (ID-AGNOSTIC)
        try:
            parsed_output = json.loads(tool_output)
            if isinstance(parsed_output, dict):
                # Automatically extract ANY field ending with "_id" or "Id"
                for key, value in parsed_output.items():
                    if (key.endswith("_id") or key.endswith("Id") or key.lower() in ["id", "uuid"]) and value is not None:
                        metadata[key] = str(value)
                
                # Also check nested dicts for IDs (one level deep)
                for key, value in parsed_output.items():
                    if isinstance(value, dict):
                        for nested_key, nested_value in value.items():
                            if (nested_key.endswith("_id") or nested_key.endswith("Id")) and nested_value is not None:
                                metadata[f"{key}.{nested_key}"] = str(nested_value)
        except (json.JSONDecodeError, KeyError, TypeError):
            pass

        self.add_memory("tool", content, metadata)

    def get_session_tool_outputs(self, session_id: str, tool_name: str = None, 
                                 n_results: int = 10, agent_id: str = None):
        """Retrieve recent tool outputs for the current session.
        
        Returns tool execution records with extracted IDs available in metadata.
        Optionally filtered by agent_id for agent-scoped isolation.
        """
        # Build filter conditions — ChromaDB requires $and for 3+ conditions
        conditions = [
            {"session_id": session_id},
            {"type": "tool_execution"},
        ]
        if tool_name:
            conditions.append({"tool_name": tool_name})
        if agent_id:
            conditions.append({"agent_id": agent_id})
        
        # ChromaDB: 1 condition = flat dict, 2+ conditions = $and
        if len(conditions) == 1:
            where_filter = conditions[0]
        else:
            where_filter = {"$and": conditions}
        
        try:
            # Query by metadata filter
            results = self.collection.get(
                where=where_filter,
                limit=n_results
            )
            return results
        except Exception as e:
            print(f"Error retrieving session tool outputs: {e}")
            return None

    def clear_memory(self):
        try:
            # Delete all items instead of dropping collection to keep UUID stable
            # fetch all ids first
            result = self.collection.get()
            if result and 'ids' in result and result['ids']:
                self.collection.delete(ids=result['ids'])
                
            print("DEBUG: Memory Store cleared (items deleted).")
            return True
        except Exception as e:
            print(f"Error clearing memory: {e}")
            # Fallback: try to recreate
            try:
                self.client.delete_collection("chat_history")
                self.collection = self.client.create_collection(name="chat_history")
                return True
            except Exception as e:
                print(f"WARNING: Failed to recreate chat_history collection: {e}")
                return False
