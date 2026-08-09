from dataclasses import dataclass

from pydantic import Discriminator

from typing import cast, Literal, Protocol, Annotated

from typing_extensions import TypedDict

from langgraph.store.base import BaseStore

from composer.spec.agent_index import AgentIndex, IndexedAgentResult, KeyedAgentResult, AgentResult, AgentIndexBase
from composer.spec.util import string_hash

class VersionedSearchResult(IndexedAgentResult):
    stale: bool

class VersionedResult(AgentResult):
    version_key: str

class VersionedRetrievalResult(AgentResult):
    caveat: str | None

class UpToDate(TypedDict):
    status: Literal["ok"]

class Stale(TypedDict):
    status: Literal["stale"]
    reason: str

type AnswerPortability = Annotated[Stale | UpToDate, Discriminator("status")]

class MigrationOracle(Protocol):
    async def __call__(
        self,
        *,
        start_version: str | None, # none means "from start"
        end_version: str,
        question: str,
        answer: str
    ) -> AnswerPortability:
        ...

@dataclass
class VersionedAgentIndex(AgentIndexBase):
    _wrapped: AgentIndex

    _store: BaseStore

    _target_ns: tuple[str, ...]

    _migration_oracle: MigrationOracle

    @classmethod
    def _lift(cls, to_lift: list[IndexedAgentResult] | KeyedAgentResult) -> list[VersionedSearchResult] | KeyedAgentResult:
        if not isinstance(to_lift, list):
            return to_lift
        return [
            { **i, "stale": False } for i in to_lift
        ]
    
    @classmethod
    def _versioned_key(
        cls, question: str, version: str
    ) -> str:
        # ``string_hash`` already returns a 16-char digest; do NOT slice it
        # further (a leftover ``[18:]`` sliced it to the empty string, so every
        # versioned entry collided at key "").
        return string_hash(f"{question}|{version}")

    async def asearch_versioned(
        self, question: str, version_list: list[str]
    ) -> list[VersionedSearchResult] | KeyedAgentResult:
        if len(version_list) == 0:
            return self._lift(await self._wrapped.asearch(question))

        key_v = self._versioned_key(question, version_list[-1])
        
        res = await self._store.aget(self._target_ns, key_v)
        if res is not None:
            to_ret : KeyedAgentResult = {
                "ref_string": key_v,
                **cast(AgentResult, res.value)
            }
            return to_ret
        
        seen : set[str] = set()
        v0_query = self._wrapped._raw_search(question)
        version_queries = [
            self._store.asearch(self._target_ns, query=question, filter={
                "version_key": version
            }) for version in version_list
        ]
        context : list[VersionedSearchResult] = []
        async for res in self.parallel_search(*[*v0_query, *version_queries]):
            if res.key in seen:
                continue
            if "version_key" not in res.value:
                # v0, base case — recorded against the unedited baseline, so any
                # applied edit may have invalidated it.
                context.append({
                    "stale": True,
                    **cast(AgentResult, res.value),
                    "ref_string": res.key,
                    "score": cast(float, res.score)
                })
            else:
                cached_version = cast(VersionedResult, res.value)
                context.append({
                    "stale": cached_version['version_key'] != version_list[-1],
                    "score": cast(float, res.score),
                    "ref_string": res.key,
                    "question": cached_version["question"],
                    "answer": cached_version["answer"]
                })
            seen.add(res.key)
            if len(context) == 5:
                break
        return context

    @classmethod
    def _caveat(cls, s: AnswerPortability) -> str | None:
        return None if s["status"] == "ok" else s["reason"]

    async def _portability_caveat(
        self, start_version: str | None, end_version: str, result: AgentResult
    ) -> str | None:
        port = await self._migration_oracle(
            start_version=start_version,
            end_version=end_version,
            question=result["question"],
            answer=result["answer"],
        )
        return self._caveat(port)

    async def aget(self, key: str, versions: list[str]) -> VersionedRetrievalResult | None:
        res = await self._wrapped.aget(key)
        if len(versions) == 0:
            if res is None:
                return None
            return { **res, "caveat": None }
        if res is not None:
            return {
                "caveat": await self._portability_caveat(None, versions[-1], res), **res
            }

        versioned_res = await self._store.aget(
            self._target_ns, key
        )
        if versioned_res is None:
            return None
        stored = cast(VersionedResult, versioned_res.value)
        if stored["version_key"] == versions[-1]:
            return {
                "question": stored["question"], "caveat": None, "answer": stored["answer"]
            }
        if stored["version_key"] not in versions:
            return None
        return {
            "answer": stored["answer"],
            "question": stored["question"],
            "caveat": await self._portability_caveat(stored["version_key"], versions[-1], stored)
        }

    async def aput(
        self, question: str, answer: str, versions: list[str]
    ) -> str | None:
        if len(versions) == 0:
            return await self._wrapped.aput(question=question, answer=answer)
        to_store : VersionedResult = {
            "answer": answer,
            "question": question,
            "version_key": versions[-1]
        }
        to_ret = self._versioned_key(question, versions[-1])
        await self._store.aput(
            self._target_ns, to_ret, { **to_store }
        )
        return to_ret

    @classmethod
    def format_context(
        cls, ctxt: list[VersionedSearchResult]
    ) -> list[str]:
        to_ret = [
f"""
--- Match {i}
**Similarity**: {res["score"]}
**Question**: {res["question"]}

**Answer**:

{res['answer']}

{"IMPORTANT: This answer was generated on an older version of this source code, you MUST verify that key details/findings are still true" if res['stale'] else ""}

--- END Match {i}
"""         for (i,res) in enumerate(ctxt, start=1)
        ]
        return to_ret
