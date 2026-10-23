/*
 * "What you can use" under an expression field (F957): the functions and methods the engine allows, from the one
 * list it decides with (`../expressionVocabulary`). Folded by default — the field's own hint says the shape; this
 * answers "is .splitlines() allowed?" before Save has to.
 */
import { EXPRESSION_VOCABULARY, RECEIVER_WORDS } from '../expressionVocabulary';

export function ExpressionVocabulary() {
    const { functions, methods } = EXPRESSION_VOCABULARY;
    return (
        <details className="-mt-1 text-xs leading-relaxed text-text-faint">
            <summary className="cursor-pointer select-none">What you can use</summary>
            <dl className="mt-1.5 space-y-1">
                <div>
                    <dt className="inline text-text-muted">Functions: </dt>
                    <dd className="inline font-code">{functions.join(', ')}</dd>
                </div>
                {Object.entries(methods).map(([kind, names]) => (
                    <div key={kind}>
                        <dt className="inline text-text-muted">{RECEIVER_WORDS[kind] ?? kind}: </dt>
                        <dd className="inline font-code">{names.map((n) => `.${n}`).join(', ')}</dd>
                    </div>
                ))}
            </dl>
        </details>
    );
}
