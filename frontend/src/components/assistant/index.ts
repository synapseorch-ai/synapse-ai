/*
 * The AI surfaces' shared pieces (M2c): one composer for chat and the AI
 * builder, the conversation's shapes, and the builder's plan as a card.
 *
 * Built here and copied byte for byte into the cloud app by its
 * `scripts/sync-ui.sh`, like `components/run/`. Nothing in this folder reads an
 * API or a product's own modules: each product passes its pickers, its Attach
 * and its answer path in.
 */
export { AgentIntro } from './AgentIntro';
export { Composer, composerPillClass, PillAvatar, type ComposerProps } from './Composer';
export { AgentAvatar, AgentTurn, RunChip, Suggestions, UserBubble } from './messages';
export { APPROVE_WORD, PlanCard, QUESTION_FOLD, QuestionCard } from './cards';
export {
    BUILDER_QUESTION_STEPS,
    questionKind,
    readPlan,
    splitQuestion,
    typeIn,
    type Plan,
    type PlanStep,
    type QuestionKind,
} from './plan';
