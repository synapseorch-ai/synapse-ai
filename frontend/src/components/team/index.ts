/*
 * A Team agent's screens (TEAM1b, D286): its setup, and its turn as the task list, the
 * notes between the lead and its members, and one lane per member.
 *
 * Built here and copied byte for byte into the cloud app by its `scripts/sync-ui.sh`,
 * like `components/assistant/`. Nothing in this folder reads an API or a product's own
 * modules: each product passes its handlers in.
 */
export { MemberQuestion, type MemberAnswer } from './MemberQuestion';
export { MemberLane, TeamLanes, type LaneHandlers } from './TeamLanes';
export { TeamNotes, TeamTasks, TeamView } from './TeamView';
export { TEAM_DEFAULTS, TeamSetup, newTeamBlock, teamMode, type TeamBlock, type TeamMode } from './TeamSetup';
export {
    LEAD_TOOLS,
    answeredHere,
    dollars,
    duration,
    emptyTeam,
    foldTeam,
    hasTeam,
    isTeamEvent,
    memberFromRecord,
    memberStatusLine,
    personNote,
    questionFrom,
    taskCount,
    teamFromTurn,
    teamSummaryLine,
    toolsBesideTeam,
    waitingQuestions,
    withAnswer,
    workingMembers,
    type MemberLaneData,
    type MemberQuestionData,
    type MemberStatus,
    type TeamNote,
    type TeamState,
    type TeamSummary,
    type TeamTask,
    type TeamTaskStatus,
} from './team';
