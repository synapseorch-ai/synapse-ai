"use client";

/*
 * A member's question, in its own lane (Q5, Q6: act where you read). The same call and
 * arguments the chat's "Permission needed" card shows, the same words to answer it.
 * TEAM1c answers it while the others work; until then the page passes `onAnswer` only
 * when the turn has paused on it, and the lane says the member needs the person.
 */
import React from 'react';

import { Button } from '@/components/ui';

import type { MemberQuestionData } from './team';

/** A person's answer to one member's question, from its lane or the chat's card (TEAM1c's ids). */
export type MemberAnswer = {
    approve: boolean;
    reason: string;
    approval_id?: string;
    member_id?: string;
    question_id: string;
};

export function MemberQuestion({
    question, memberId, memberName, onAnswer, disabled = false,
}: {
    question: MemberQuestionData;
    memberId: string;
    memberName: string;
    /** Answer it; absent while it cannot be answered from here yet. */
    onAnswer?: (answer: MemberAnswer) => void;
    disabled?: boolean;
}) {
    const [reason, setReason] = React.useState('');
    const args = question.toolArgs && Object.keys(question.toolArgs).length > 0 ? question.toolArgs : null;
    const answer = (approve: boolean) => onAnswer?.({
        approve, reason: approve ? '' : reason, approval_id: question.approvalId, member_id: memberId,
        question_id: question.questionId,
    });

    return (
        <div role="group" aria-label={`${memberName} asks: run ${question.toolName}?`}
             className="flex min-w-0 flex-col gap-2 rounded-lg border border-person/60 bg-surface px-3 py-2.5">
            <span className="inline-flex items-center gap-1.5 text-xs font-medium text-person-ink">
                <span aria-hidden className="size-2 rounded-full bg-person" />
                Permission needed
            </span>
            <p className="text-sm text-text">
                Run <span className="font-code text-[13px]">{question.toolName}</span>?
            </p>
            {args && (
                <pre className="max-h-40 overflow-auto rounded-md border border-border bg-surface-2 p-2 font-code text-xs text-text">
                    {JSON.stringify(args, null, 2)}
                </pre>
            )}
            {question.answered ? (
                <p className="text-xs text-text-faint">
                    {question.answered === 'approved' ? 'Allowed.' : 'Refused.'}
                </p>
            ) : onAnswer ? (
                <>
                    <input
                        value={reason}
                        onChange={(e) => setReason(e.target.value)}
                        aria-label={`Reason for refusing ${memberName}`}
                        placeholder="Why not? (optional)"
                        disabled={disabled}
                        className="w-full rounded-md border border-border-strong bg-surface px-2.5 py-1.5 text-sm text-text placeholder:text-text-faint disabled:opacity-50"
                    />
                    <div className="flex flex-wrap gap-2">
                        <Button type="button" variant="person" size="sm" disabled={disabled} onClick={() => answer(true)}>
                            Allow once
                        </Button>
                        <Button type="button" variant="secondary" size="sm" disabled={disabled} onClick={() => answer(false)}>
                            Refuse
                        </Button>
                    </div>
                    <p className="text-xs text-text-faint">The other members keep working while this waits.</p>
                </>
            ) : (
                <p className="text-xs text-text-faint">
                    The other members keep working. You can answer when the turn pauses for you.
                </p>
            )}
        </div>
    );
}
