/**
 * A custom tool's output schema as its editor reads and writes it (F1620): the text of `outputSchema` in the draft,
 * read into answer fields as the server reads a declaration (`output_fields.from_json_schema`), and fields written
 * back as the server compiles them (`json_schema`), through F1618's `answerFields.ts`.
 *
 * A schema inside the answer's subset gives its fields. One with a part outside it gives none, and each part named
 * (`kept`): the editor shows it read-only and saves it as it is, never rewritten. No schema gives no fields, and no
 * fields give no text, so no `outputSchema` is sent.
 */
import { answerFieldsFromSchema, answerJsonSchema, type AnswerField } from '@/components/orchestration/answerFields';

export type ToolAnswer = { fields: AnswerField[]; kept: string[] };

export function toolAnswerOf(text: string | null | undefined): ToolAnswer {
    if (!text || !text.trim()) return { fields: [], kept: [] };
    let schema: unknown;
    try {
        schema = JSON.parse(text);
    } catch (err) {
        return { fields: [], kept: [`It is not JSON: ${(err as Error).message}`] };
    }
    const read = answerFieldsFromSchema(schema);
    return read.refused.length ? { fields: [], kept: read.refused } : { fields: read.fields, kept: [] };
}

export function toolAnswerText(fields: AnswerField[]): string {
    return fields.length ? JSON.stringify(answerJsonSchema(fields), null, 2) : '';
}
