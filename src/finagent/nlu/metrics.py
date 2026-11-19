"""Shared semantic metrics: failures remain in denominators; enums are frozen."""
from collections import Counter
from .semantic import SCHEMA


def entity_keys(value, security_only=False):
    return {(e["type"], e["role"], e["start"], e["end"]) for e in value["entities"]
            if not security_only or e["type"] in ("COMPANY", "STOCK_CODE")} if value else set()


def exact(gold, predicted):
    return bool(predicted and all(predicted[k] == gold[k] for k in ("task_type", "dialogue_act", "entity_binding", "dimension_state"))
                and entity_keys(gold) == entity_keys(predicted))


def classification(gold, predicted, classes):
    per_class = {}
    for label in classes:
        tp = sum(g == label and p == label for g, p in zip(gold, predicted))
        fp = sum(g != label and p == label for g, p in zip(gold, predicted))
        fn = sum(g == label and p != label for g, p in zip(gold, predicted))
        per_class[label] = {"support": gold.count(label), "precision": tp / (tp + fp) if tp + fp else 0,
                            "recall": tp / (tp + fn) if tp + fn else 0,
                            "f1": 2 * tp / (2 * tp + fp + fn) if 2 * tp + fp + fn else 0}
    return {"accuracy": sum(g == p for g, p in zip(gold, predicted)) / len(gold) if gold else None,
            "macro_f1": sum(v["f1"] for v in per_class.values()) / len(classes), "per_class": per_class,
            "confusion": dict(Counter(f"{g} -> {p}" for g, p in zip(gold, predicted)))}


def semantic_metrics(golds, predictions):
    if not golds or len(golds) != len(predictions):
        raise ValueError("Metric inputs must be nonempty and have identical lengths")
    properties = SCHEMA["properties"]
    metrics = {name: classification([g[name] for g in golds], [p[name] if p else "INVALID" for p in predictions], properties[name]["enum"])
               for name in ("task_type", "dialogue_act", "entity_binding")}
    dimensions = properties["dimension_state"]["properties"]
    metrics["dimensions"] = {d: classification([g["dimension_state"][d] for g in golds],
           [p["dimension_state"][d] if p else "INVALID" for p in predictions], spec["enum"]) for d, spec in dimensions.items()}
    tp = fp = fn = securities = securities_correct = no_security = false_security = 0
    for gold, prediction in zip(golds, predictions):
        g, p = entity_keys(gold), entity_keys(prediction)
        tp += len(g & p); fp += len(p - g); fn += len(g - p)
        g, p = entity_keys(gold, True), entity_keys(prediction, True)
        if g:
            securities += 1; securities_correct += bool(prediction and g == p)
        else:
            no_security += 1; false_security += bool(p)
    metrics.update(count=len(golds), valid_output_rate=sum(p is not None for p in predictions) / len(golds),
                   exact_frame=sum(exact(g, p) for g, p in zip(golds, predictions)) / len(golds),
                   five_dimension_exact_match=sum(bool(p and g["dimension_state"] == p["dimension_state"]) for g, p in zip(golds, predictions)) / len(golds),
                   entity_span_f1=2 * tp / (2 * tp + fp + fn) if 2 * tp + fp + fn else None,
                   security_set_exact_match=securities_correct / securities if securities else None,
                   no_security_false_extraction=false_security / no_security if no_security else None)
    return metrics


def semantic_gates(metrics):
    def at_least(value, target):
        return value is not None and value >= target
    supported = all(v["support"] >= 10 for name in ("task_type", "dialogue_act", "entity_binding") for v in metrics[name]["per_class"].values())
    dimensions_ok = all(d["macro_f1"] >= .85 and all(d["per_class"][c]["f1"] >= .80 and d["per_class"][c]["support"] >= 20
                       for c in ("include", "exclude")) for d in metrics["dimensions"].values())
    return (metrics["count"] >= 300 and supported and dimensions_ok and metrics["valid_output_rate"] >= .99
            and metrics["task_type"]["macro_f1"] >= .90
            and metrics["dialogue_act"]["macro_f1"] >= .85 and metrics["entity_binding"]["macro_f1"] >= .85
            and at_least(metrics["entity_span_f1"], .90) and at_least(metrics["security_set_exact_match"], .95)
            and metrics["exact_frame"] >= .80)
