"""Render saved review measurements and optional external labels as portable HTML.

This offline CLI reads experiment/judgment JSON, checks the experiment hash, and
writes an HTML report plus aggregate JSON. It never calls a model or retrieval.
"""

import argparse
import hashlib
from html import escape
import json
from pathlib import Path

from evals.experiments.review_metrics import VARIANTS, summarize

NAMES = {"baseline": "Antes del juez", "review_only": "Solo primera revisión", "final": "Flujo completo"}


def render_report(report, judgments=None):
    """Build a self-contained report; escape all model-generated/user text as data."""
    judgments = judgments or {}
    summary = summarize(report["samples"], judgments.get("samples"))
    sections = []
    for cohort, metrics in summary.items():
        rows = []
        for variant, values in metrics["variants"].items():
            if values["model_calls"] is None:
                continue
            quality = metrics["quality"]
            score = 'Pendiente'
            if quality:
                score = f'{quality["task_success_rate"][variant]:.1%}'
            rows.append(f'<tr><th>{NAMES[variant]}</th><td>{score}</td>'
                        f'<td>{values["model_calls"]["mean"]:.2f}</td>'
                        f'<td>{values["real_search_calls"]["mean"]:.2f}</td>'
                        f'<td>{values["elapsed_ms"]["mean"] / 1000:.2f} s</td>'
                        f'<td>{values["elapsed_ms"]["p95"] / 1000:.2f} s</td>'
                        f'<td>{values["input_tokens_reported"]:,} / {values["output_tokens_reported"]:,}</td></tr>')
        title = 'Preguntas naturales' if cohort == 'natural' else 'Controles con fallos introducidos'
        quality_note = 'La calidad todavía no tiene anotaciones externas.'
        if metrics['quality']:
            q = metrics['quality']
            quality_note = (f'{q["labeled_pairs"]}/{q["completed_pairs"]} pares anotados. '
                            f'Respuestas corregidas: {q["corrected_answers"]}; correctas perdidas: {q["lost_correct_answers"]}; '
                            f'rechazos más seguros: {q["safer_rejections"]}; rechazos de borradores correctos: {q["false_rejections"]}; '
                            f'respuestas incorrectas introducidas: {q["introduced_wrong_answers"]}.')
        sections.append(f'<section><h2>{title}</h2><p>{metrics["completed"]}/{metrics["scheduled"]} ejecuciones completadas; '
                        f'{metrics["failed"]} errores. {metrics["reviewed"]} pasaron por el juez; '
                        f'{metrics["repaired"]} usaron recuperación adicional.</p>'
                        '<div class="scroll"><table><thead><tr><th>Resultado pareado</th><th>Éxito de tarea</th>'
                        '<th>Llamadas modelo · media</th><th>Búsquedas reales · media</th><th>Tiempo · media</th>'
                        '<th>Tiempo · p95</th><th>Tokens entrada / salida · total</th></tr></thead>'
                        f'<tbody>{"".join(rows)}</tbody></table></div><p>{quality_note}</p>'
                        f'<p>Incumplimientos: rondas {metrics["round_limit_violations"]}, '
                        f'consultas {metrics["query_limit_violations"]}, contexto {metrics["context_budget_violations"]}.</p></section>')
    details = []
    labels = judgments.get('samples', {})
    for sample in report['samples']:
        sample_id = escape(sample['sample_id'])
        cohort = escape(sample['cohort'])
        if sample['status'] != 'ok':
            details.append(f'<details data-cohort="{cohort}"><summary>{sample_id} · error</summary>'
                           f'<p>{escape(sample["error_type"])}</p></details>')
            continue
        case_labels = labels.get(sample['sample_id'], {})
        answers = []
        for variant in VARIANTS:
            value = sample['variants'][variant]
            label = escape(case_labels.get(variant, 'Sin valorar'))
            answers.append(f'<h4>{NAMES[variant]} · {label}</h4><p class="answer">{escape(value["answer"])}</p>')
        objections = [objection for verdict in sample['reviews']
                      for field in ('unsupported_claims', 'missing_information') for objection in verdict[field]]
        note = case_labels.get('note', '')
        details.append(f'<details data-cohort="{cohort}"><summary>{sample_id} · '
                       f'{sample["additional_retrievals"]} ronda adicional · '
                       f'{sample["variants"]["final"]["model_calls"]} llamadas</summary>'
                       f'<h3>{escape(sample["question"])}</h3>{"".join(answers)}'
                       f'<p><strong>Valoración externa:</strong> {escape(note)}</p>'
                       f'<p><strong>Objeciones del juez:</strong> {escape(" | ".join(objections))}</p></details>')
    settings = escape(json.dumps(report['settings'], ensure_ascii=False))
    rater = escape(judgments.get('rater', 'Sin valoración externa.'))
    grading_policy = escape(judgments.get('grading_policy', ''))
    html = '''<!doctype html><html lang="es"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Evaluación de revisión de respuestas</title>
<style>
:root{color-scheme:light dark}body{font:15px/1.55 system-ui;color:var(--foreground,light-dark(#172033,#e6ebf4));margin:0}
h1{font-size:25px}h2{font-size:19px;margin-top:26px}h3{font-size:16px}h4{margin-bottom:5px}
p{max-width:100ch}.scroll{overflow:auto}table{width:100%;border-collapse:collapse;font-size:13px}
th,td{padding:10px 8px;text-align:left;border-bottom:1px solid var(--border,#9ba5b5)}
thead th{vertical-align:bottom}td{font-variant-numeric:tabular-nums}details{border-top:1px solid var(--border,#9ba5b5);padding:12px 0}
summary{cursor:pointer;font-weight:600}.answer{white-space:pre-wrap}select{font:inherit;padding:6px;margin:12px 0}
.small{font-size:13px;color:var(--muted-foreground,light-dark(#657084,#aab4c4))}code{overflow-wrap:anywhere}
</style><body><h1>Revisión de respuestas: utilidad y coste observado</h1>
<p>Comparación pareada de tres estados de una ejecución: borrador validado, parada tras el primer juez y resultado completo.
La parada intermedia es contrafactual; no es otra ejecución independiente. Cambiar o aprobar una respuesta no demuestra mejorarla.</p>
'''+ ''.join(sections) + f'''<p class="small">Configuración: <code>{settings}</code>.
Tres repeticiones de la misma pregunta no son tres preguntas independientes. Tokens reportados por el proveedor;
no incluyen embeddings ni estiman precio. Tiempos acumulados desde el inicio; p95 por rango observado.</p>
<p class="small">Valoración: {rater} Éxito exige respuesta correcta en casos respondibles, abstención cuando falta el documento,
o aclaración cuando falta la pregunta. Una respuesta errónea que pasa a abstención es más segura, pero no una respuesta corregida.</p>
<p class="small">{grading_policy}</p>
<h2>Inspección de cada par</h2><label>Mostrar <select id="cohort"><option value="all">Todos los casos</option>
<option value="natural">Preguntas naturales</option><option value="controlled">Controles</option></select></label>
'''+ ''.join(details) + '''<script>
// Filter saved samples locally; answers and judgments are never sent elsewhere.
document.getElementById('cohort').addEventListener('change', event => {
  document.querySelectorAll('details[data-cohort]').forEach(row => {
    row.hidden = event.target.value !== 'all' && row.dataset.cohort !== event.target.value;
  });
});
console.log('Report ready', document.querySelectorAll('details').length, 'samples');
</script></body></html>'''
    return html, summary


def main():
    """Regenerate the portable artifact and summary with no paid calls."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, default=Path('data/answer_review/experiment.json'))
    parser.add_argument('--judgments', type=Path)
    parser.add_argument('--output', type=Path, default=Path('data/answer_review/report.html'))
    args = parser.parse_args()
    raw = args.input.read_bytes()
    report = json.loads(raw)
    judgments = json.loads(args.judgments.read_text()) if args.judgments else None
    if judgments and judgments['report_sha256'] != hashlib.sha256(raw).hexdigest():
        parser.error('Judgments belong to a different experiment snapshot')
    html, summary = render_report(report, judgments)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(html)
    args.output.with_suffix('.summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
