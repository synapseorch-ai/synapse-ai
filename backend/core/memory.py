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
        self.model = model
        self._embed_fn = embed_fn
        print(f"DEBUG: MemoryStore initialized at {self.storage_path} with model {self.model}")

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
        
        if self._embed_fn:
            try:
                return self._embed_fn(text)
            except Exception as e:
                print(f"Error getting embedding from configured provider: {e}")
                return None
        try:
            # Default: Use Ollama for embeddings (best-effort)
            import ollama

            from core.config import load_settings
            from core.llm_providers import _ollama_base_url

            # The setting, not only the ollama library's own OLLAMA_HOST (F76).
            client = ollama.Client(host=_ollama_base_url(load_settings()))
            response = client.embeddings(model=self.model, prompt=text)
            return response["embedding"]
        except Exception as e:
            print(f"Error getting embedding from Ollama: {e}")
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

    # ========================================================================
    # DYNAMIC SESSION-SCOPED RAG
    # Embeddings created on-demand for vague queries, auto-cleaned after session
    # ========================================================================
    
    def embed_report_for_session(
        self,
        session_id: str,
        report_data: list[dict],
        report_type: str,
        chunk_size: int = 50
    ) -> dict:
        """
        Create temporary embeddings for a report, scoped to current session.
        
        Used for exploratory/vague queries where semantic search is beneficial.
        Embeddings are automatically cleaned up when session ends.
        
        Args:
            session_id: Current session ID
            report_data: List of records from the report
            report_type: Type of report (orders, payments, etc.)
            chunk_size: Rows per chunk
        
        Returns:
            {
              "collection_name": str,
              "chunks_embedded": int,
              "total_rows": int
            }
        """
        if not report_data:
            return {"error": "No report data provided"}
        
        # Create unique collection name for this session + report
        collection_name = f"session_{session_id}_{report_type}_{datetime.now().timestamp()}"
        
        try:
            # Create ephemeral collection
            session_collection = self.client.get_or_create_collection(collection_name)
            
            # Chunk the report data
            chunks = []
            for i in range(0, len(report_data), chunk_size):
                chunks.append(report_data[i:i + chunk_size])
            
            print(f"DEBUG: Embedding {len(chunks)} chunks for session {session_id}")
            
            # Embed each chunk
            embedded_count = 0
            for i, chunk in enumerate(chunks):
                # Create semantic summary for better search
                chunk_text = self._create_semantic_chunk_summary(
                    chunk, 
                    report_type,
                    chunk_index=i,
                    total_chunks=len(chunks)
                )
                
                # Pre-truncate chunks to stay within token limits
                # get_embedding will do final truncation to 20,000 chars (~6,600 tokens)
                # But we pre-truncate here to 12,000 chars (~4,000 tokens) for better chunk quality
                MAX_CHUNK_CHARS = 12000
                if len(chunk_text) > MAX_CHUNK_CHARS:
                    chunk_text = chunk_text[:MAX_CHUNK_CHARS] + "\n... (chunk truncated)"
                    print(f"DEBUG: Pre-truncated chunk {i} summary to {MAX_CHUNK_CHARS} chars")
                
                # Generate embedding
                embedding = self.get_embedding(chunk_text)
                if not embedding:
                    print(f"WARNING: Failed to embed chunk {i}")
                    continue
                
                # Store chunk with metadata
                session_collection.add(
                    ids=[f"chunk_{i}"],
                    embeddings=[embedding],
                    documents=[json.dumps(chunk)],  # Store actual data
                    metadatas=[{
                        "chunk_index": i,
                        "row_count": len(chunk),
                        "report_type": report_type,
                        "session_id": session_id,
                        "timestamp": datetime.now().isoformat()
                    }]
                )
                embedded_count += 1
            
            print(f"DEBUG: Successfully embedded {embedded_count}/{len(chunks)} chunks")
            
            return {
                "collection_name": collection_name,
                "chunks_embedded": embedded_count,
                "total_rows": len(report_data),
                "chunk_size": chunk_size
            }
            
        except Exception as e:
            print(f"Error embedding report for session: {e}")
            return {"error": str(e)}
    
    def search_session_embeddings(
        self,
        session_id: str,
        query: str,
        n_results: int = 3,
        collection_name: str = None
    ) -> list[dict]:
        """
        HYBRID search: exact text match FIRST, then semantic fallback.
        
        Semantic search is great for meaning-based queries but terrible for exact identifiers ("A101", "unit 204")
        because short codes like "A101" are nearly equidistant from "B101", "C101"
        in embedding space — they have no semantic meaning, just label similarity.
        
        Strategy:
          1. Extract potential identifiers from the query (alphanumeric codes, numbers)
          2. Do a direct text scan of stored chunk documents for exact matches
          3. If exact matches found → return those chunks (no embedding needed)
          4. If no exact matches → fall back to semantic (embedding) search
        
        Args:
            session_id: Current session ID
            query: Natural language search query
            n_results: Max chunks to return
            collection_name: Specific collection to search (optional)
        
        Returns:
            List of matching chunks with similarity scores
        """
        import re as _re
        
        try:
            # Find all session collections
            if collection_name:
                collection_names = [collection_name]
            else:
                all_collections = self.client.list_collections()
                collection_names = [
                    c.name for c in all_collections 
                    if c.name.startswith(f"session_{session_id}_")
                ]
            
            if not collection_names:
                print(f"DEBUG: No session embeddings found for {session_id}")
                return []
            
            # ── PHASE 1: Extract identifiers from the query ──
            identifier_patterns = _re.findall(
                r'\b[A-Za-z]?\d+[A-Za-z]?\b'    # alphanumeric codes: A101, 204, B12
                r'|[A-Za-z]\-?\d+'                # hyphenated: A-101, B-12
                r'|"[^"]{1,30}"'                  # quoted strings: "John Smith"
                r"|'[^']{1,30}'",                 # single-quoted strings
                query
            )
            # Also grab multi-word proper nouns / specific terms from the query
            # (strip common filler words)
            filler = {"tell", "me", "about", "the", "and", "its", "it's", "of",
                       "for", "in", "at", "to", "a", "an", "this", "that", "show",
                       "get", "find", "search", "what", "is", "are", "details",
                       "info", "information", "data", "report", "please", "can", "you", "give", "list"}
            query_keywords = [
                w.strip("\"'") for w in query.split()
                if w.strip("\"'").lower() not in filler and len(w.strip("\"'")) >= 2
            ]
            
            search_terms = list(set(identifier_patterns + query_keywords))
            print(f"DEBUG: 🔍 Hybrid search — identifiers extracted: {search_terms}")
            
            # ── PHASE 2: Exact text scan across all chunks ──
            exact_results = []
            if search_terms:
                for coll_name in collection_names:
                    collection = self.client.get_collection(coll_name)
                    # Get ALL documents from this collection
                    all_docs = collection.get(include=["documents", "metadatas"])
                    
                    if not all_docs or not all_docs.get("documents"):
                        continue
                    
                    for idx, doc_text in enumerate(all_docs["documents"]):
                        doc_lower = doc_text.lower()
                        # Check if ANY search term appears in the raw JSON
                        matched_terms = [
                            t for t in search_terms
                            if t.lower() in doc_lower
                        ]
                        if matched_terms:
                            try:
                                chunk_data = json.loads(doc_text)
                            except Exception:
                                chunk_data = doc_text
                            
                            exact_results.append({
                                "chunk_data": chunk_data,
                                "similarity_score": 1.0,  # Perfect match
                                "metadata": all_docs["metadatas"][idx] if all_docs.get("metadatas") else {},
                                "collection": coll_name,
                                "match_type": "exact",
                                "matched_terms": matched_terms,
                            })
                            print(f"DEBUG: ✅ EXACT MATCH in {coll_name} chunk {idx} — matched: {matched_terms}")
            
            if exact_results:
                print(f"DEBUG: 🎯 Found {len(exact_results)} exact matches — skipping semantic search")
                # Deduplicate and return top N
                return exact_results[:n_results]
            
            # ── PHASE 3: Semantic fallback (no exact matches found) ──
            print(f"DEBUG: 🔄 No exact matches — falling back to semantic search")
            query_embedding = self.get_embedding(query)
            if not query_embedding:
                return []
            
            all_results = []
            for coll_name in collection_names:
                collection = self.client.get_collection(coll_name)
                
                results = collection.query(
                    query_embeddings=[query_embedding],
                    n_results=n_results
                )
                
                if results and results.get('documents') and results['documents'][0]:
                    for i, doc in enumerate(results['documents'][0]):
                        all_results.append({
                            "chunk_data": json.loads(doc),
                            "similarity_score": 1 - results['distances'][0][i],
                            "metadata": results['metadatas'][0][i],
                            "collection": coll_name,
                            "match_type": "semantic",
                        })
            
            all_results.sort(key=lambda x: x['similarity_score'], reverse=True)
            return all_results[:n_results]
            
        except Exception as e:
            print(f"Error searching session embeddings: {e}")
            return []
    
    def search_embedded_report(
        self,
        session_id: str,
        query: str,
        n_results: int = 5,
    ) -> dict:
        """
        Public API wrapper around search_session_embeddings.
        
        Returns a dict with 'results' key (expected by chat.py handlers)
        instead of a raw list.
        """
        raw_results = self.search_session_embeddings(
            session_id=session_id,
            query=query,
            n_results=n_results,
        )
        return {"results": raw_results}

    def clear_session_embeddings(self, session_id: str) -> int:
        """
        Delete all session-scoped embeddings for cleanup.
        
        Called when session ends or user explicitly clears context.
        
        Returns:
            Number of collections deleted
        """
        try:
            # Find all session collections
            all_collections = self.client.list_collections()
            session_prefix = f"session_{session_id}_"
            
            deleted_count = 0
            for collection in all_collections:
                if collection.name.startswith(session_prefix):
                    try:
                        self.client.delete_collection(collection.name)
                        deleted_count += 1
                        print(f"DEBUG: Deleted session collection {collection.name}")
                    except Exception as e:
                        print(f"Error deleting collection {collection.name}: {e}")
            
            if deleted_count > 0:
                print(f"DEBUG: Cleared {deleted_count} session embedding collections")
            
            return deleted_count
            
        except Exception as e:
            print(f"Error clearing session embeddings: {e}")
            return 0
    
    # Columns likely to contain unique identifiers for each row.
    # These are listed verbatim in the chunk summary so semantic search
    # can match specific entities.
    _IDENTITY_KEYWORDS = {
        "name", "unit", "space", "resident", "occupant",
        "address", "email", "phone", "id", "code", "number", "label",
        "description", "title", "type", "status", "category",
    }

    def _is_identity_column(self, col_name: str) -> bool:
        """Return True if a column likely identifies individual rows."""
        lower = col_name.lower().replace("_", " ").replace("-", " ")
        return any(kw in lower for kw in self._IDENTITY_KEYWORDS)

    def _create_semantic_chunk_summary(
        self,
        chunk: list[dict],
        report_type: str,
        chunk_index: int = 0,
        total_chunks: int = 1
    ) -> str:
        """
        Create rich semantic text representation of a chunk for embedding.
        
        KEY DESIGN PRINCIPLE: For identity/name columns, list ALL unique values
        (not just top 3). This is critical because the embedding vector is the
        ONLY thing used for similarity search — if "unit 204" doesn't appear
        in the summary text, the embedding won't match a query about unit 204.
        
        For numeric columns, provide aggregate statistics.
        """
        if not chunk:
            return ""
        
        try:
            import pandas as pd
        except ImportError:
            return self._simple_chunk_summary(chunk, report_type)
        
        df = pd.DataFrame(chunk)
        
        summary_parts = [
            f"Report Type: {report_type}",
            f"Chunk {chunk_index + 1} of {total_chunks}",
            f"Contains {len(chunk)} records"
        ]
        
        # Separate columns into identity vs stats
        for col in df.columns:
            try:
                if pd.api.types.is_numeric_dtype(df[col]):
                    summary_parts.append(
                        f"{col}: ranges from {df[col].min()} to {df[col].max()}, "
                        f"average {df[col].mean():.2f}, total {df[col].sum():.2f}"
                    )
                elif pd.api.types.is_object_dtype(df[col]) or pd.api.types.is_string_dtype(df[col]):
                    unique_vals = df[col].dropna().unique()
                    if len(unique_vals) == 0:
                        continue
                    
                    if self._is_identity_column(col):
                        # IDENTITY COLUMNS: List ALL values so search can match any
                        # This is the critical fix — previously only top 3 were listed
                        vals_str = ", ".join(str(v) for v in unique_vals)
                        summary_parts.append(f"{col} (all values): {vals_str}")
                    else:
                        # Non-identity categoricals: top values + count
                        top_values = df[col].value_counts().head(5)
                        if not top_values.empty:
                            summary_parts.append(
                                f"{col}: {', '.join(str(v) for v in top_values.index)} "
                                f"({len(unique_vals)} unique)"
                            )
            except Exception:
                continue
        
        # Add a few sample records for structural context
        summary_parts.append("\nSample Records:")
        for i, row in enumerate(chunk[:3], 1):
            record_text = ", ".join(
                f"{k}={v}" for k, v in row.items() 
                if v is not None and str(v).strip()
            )
            summary_parts.append(f"{i}. {record_text[:300]}")
        
        return "\n".join(summary_parts)
    
    def _simple_chunk_summary(self, chunk: list[dict], report_type: str) -> str:
        """Fallback summary method when pandas not available."""
        summary = f"Report: {report_type}, {len(chunk)} records\n"
        for i, record in enumerate(chunk[:3], 1):
            record_text = ", ".join(f"{k}={v}" for k, v in record.items() if v)
            summary += f"{i}. {record_text[:200]}\n"
        return summary

    # ========================================================================
    # SMART REPORT SUMMARY (for large reports that exceed context limits)
    # ========================================================================

    @staticmethod
    def generate_report_summary(report_data: list[dict], report_type: str, max_sample_rows: int = 5) -> dict:
        """
        Generate a compact but informative summary of a large report.
        
        Used when the full report is too large to fit in the LLM context window.
        The summary includes:
          - Pre-computed aggregations (sum, avg, min, max) for numeric columns
          - Value distributions for categorical columns
          - Sample rows for structure understanding
          - Metadata (row count, column names)
        
        This allows the LLM to answer aggregation questions directly from the summary,
        and use search_embedded_report for specific row lookups.
        
        Args:
            report_data: Full list of report records
            report_type: Type of report (e.g., "orders", "payments")
            max_sample_rows: Number of sample rows to include
        
        Returns:
            dict with summary info, suitable for JSON serialization
        """
        if not report_data:
            return {"error": "No data to summarize"}

        columns = list(report_data[0].keys()) if report_data else []
        total_rows = len(report_data)

        # Try pandas for richer analysis
        try:
            import pandas as pd
            df = pd.DataFrame(report_data)

            aggregations = {}
            categorical_distributions = {}

            for col in df.columns:
                try:
                    if pd.api.types.is_numeric_dtype(df[col]):
                        col_stats = {
                            "min": round(float(df[col].min()), 2),
                            "max": round(float(df[col].max()), 2),
                            "mean": round(float(df[col].mean()), 2),
                            "sum": round(float(df[col].sum()), 2),
                            "median": round(float(df[col].median()), 2),
                            "non_null_count": int(df[col].notna().sum()),
                        }
                        # Add count of zeros and negatives if relevant
                        zeros = int((df[col] == 0).sum())
                        negatives = int((df[col] < 0).sum())
                        if zeros > 0:
                            col_stats["zero_count"] = zeros
                        if negatives > 0:
                            col_stats["negative_count"] = negatives
                        aggregations[col] = col_stats
                    elif pd.api.types.is_object_dtype(df[col]) or pd.api.types.is_string_dtype(df[col]):
                        value_counts = df[col].value_counts().head(10)
                        if not value_counts.empty:
                            categorical_distributions[col] = {
                                "unique_values": int(df[col].nunique()),
                                "top_values": {str(k): int(v) for k, v in value_counts.items()},
                                "null_count": int(df[col].isna().sum()),
                            }
                except Exception:
                    continue

            summary = {
                "is_summary": True,
                "report_type": report_type,
                "total_rows": total_rows,
                "columns": columns,
                "numeric_aggregations": aggregations,
                "categorical_distributions": categorical_distributions,
                "sample_rows": report_data[:max_sample_rows],
                "note": (
                    f"This is a SUMMARY of {total_rows} rows. Full data is embedded in RAG memory. "
                    "Use the pre-computed aggregations above to answer totals/averages/min/max questions. "
                    "For specific row lookups, use search_embedded_report tool."
                ),
            }

        except ImportError:
            # Fallback without pandas
            aggregations = {}
            for col in columns:
                values = [r.get(col) for r in report_data if r.get(col) is not None]
                numeric_values = []
                for v in values:
                    try:
                        numeric_values.append(float(v))
                    except (ValueError, TypeError):
                        pass
                if numeric_values:
                    aggregations[col] = {
                        "min": round(min(numeric_values), 2),
                        "max": round(max(numeric_values), 2),
                        "mean": round(sum(numeric_values) / len(numeric_values), 2),
                        "sum": round(sum(numeric_values), 2),
                        "count": len(numeric_values),
                    }

            summary = {
                "is_summary": True,
                "report_type": report_type,
                "total_rows": total_rows,
                "columns": columns,
                "numeric_aggregations": aggregations,
                "sample_rows": report_data[:max_sample_rows],
                "note": (
                    f"This is a SUMMARY of {total_rows} rows. Full data is embedded in RAG memory. "
                    "Use the pre-computed aggregations above to answer totals/averages/min/max questions. "
                    "For specific row lookups, use search_embedded_report tool."
                ),
            }

        return summary

