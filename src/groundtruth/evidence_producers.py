"""Evidence producers for the GT substrate runtime path."""

from __future__ import annotations

from collections import Counter

from groundtruth.substrate.types import EvidenceItem


class SiblingProducer:
    """Produce behavioral consistency evidence from sibling methods/functions."""

    family = "SIBLING"

    def produce(self, reader, target_node_id: int, root: str) -> list[EvidenceItem]:  # noqa: ANN001, ARG002
        target = reader.get_node_by_id(target_node_id)
        if not target:
            return []

        siblings = reader.get_siblings(target_node_id)
        if len(siblings) < 2:
            return []

        target_name = target["name"]
        file_path = target.get("file_path", "")
        start_line = target.get("start_line", 0)
        results: list[EvidenceItem] = []

        ret_types = [s.get("return_type") for s in siblings if s.get("return_type")]
        if ret_types:
            common_rt, count = Counter(ret_types).most_common(1)[0]
            fraction = count / len(siblings)
            if fraction >= 0.7:
                target_rt = target.get("return_type")
                if target_rt and target_rt != common_rt:
                    results.append(EvidenceItem(
                        family=self.family,
                        score=3,
                        name=target_name,
                        file=file_path,
                        line=start_line,
                        source_code="",
                        summary=(
                            f"INCONSISTENCY: {count}/{len(siblings)} siblings return "
                            f"{common_rt}, but {target_name} returns {target_rt}"
                        ),
                        confidence=0.90,
                        tier="verified",
                    ))
                else:
                    results.append(EvidenceItem(
                        family=self.family,
                        score=2,
                        name=target_name,
                        file=file_path,
                        line=start_line,
                        source_code="",
                        summary=(
                            f"{count}/{len(siblings)} siblings return {common_rt} "
                            "preserve this convention"
                        ),
                        confidence=0.80,
                        tier="likely",
                    ))

        param_counts: list[int] = []
        for sibling in siblings:
            signature = sibling.get("signature", "")
            if not signature:
                continue
            params = signature.split("(", 1)[-1].rstrip(")")
            count = len(
                [p for p in params.split(",") if p.strip() and p.strip() != "self"]
            ) if params.strip() else 0
            param_counts.append(count)

        if param_counts:
            common_pc, count = Counter(param_counts).most_common(1)[0]
            if count / len(siblings) >= 0.8:
                target_params = 0
                target_sig = target.get("signature", "")
                if target_sig:
                    params = target_sig.split("(", 1)[-1].rstrip(")")
                    target_params = len(
                        [p for p in params.split(",") if p.strip() and p.strip() != "self"]
                    ) if params.strip() else 0
                if target_params != common_pc:
                    results.append(EvidenceItem(
                        family=self.family,
                        score=2,
                        name=target_name,
                        file=file_path,
                        line=start_line,
                        source_code="",
                        summary=(
                            f"{count}/{len(siblings)} siblings take {common_pc} params, "
                            f"{target_name} takes {target_params}"
                        ),
                        confidence=0.80,
                        tier="likely",
                    ))

        sibling_exceptions: list[str] = []
        for sibling in siblings:
            sibling_id = sibling.get("id")
            if sibling_id is None:
                continue
            for prop in reader.get_properties(sibling_id, kind="exception_type"):
                value = prop.get("value", "")
                if value:
                    sibling_exceptions.append(value)
        if sibling_exceptions:
            common_exc, count = Counter(sibling_exceptions).most_common(1)[0]
            if count >= 2:
                results.append(EvidenceItem(
                    family=self.family,
                    score=2,
                    name=target_name,
                    file=file_path,
                    line=start_line,
                    source_code="",
                    summary=(
                        f"{count} siblings raise {common_exc} "
                        f"{target_name} should follow this pattern"
                    ),
                    confidence=0.78,
                    tier="likely",
                ))

        if not results:
            return [EvidenceItem(
                family=self.family,
                score=1,
                name=target_name,
                file=file_path,
                line=start_line,
                source_code="",
                summary=f"sibling method in same class ({len(siblings)} total)",
                confidence=0.72,
                tier="likely",
            )]

        results.sort(key=lambda item: (-item.score, -item.confidence))
        return results[:2]
