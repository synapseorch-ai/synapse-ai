'use client';

/*
 * What the Phone tab needs from the product it runs in (VOX1c).
 *
 * `components/phone/` is copied byte for byte into the cloud app, so nothing in
 * it may know a URL, a fetch helper or a store. What differs arrives through
 * this context, which each product's page provides from a module-level value:
 * OSS's in `lib/phoneHost.tsx`, cloud's in `app/org/channels/phone/PhoneClient.tsx`.
 *
 * - How numbers, calls and a call's conversation are read: OSS through its
 *   generic channel routes and session history, cloud through its own number
 *   routes and chat sessions. Both hand back the shapes in `phone.ts`.
 * - How a number is connected and saved. Each write resolves with the number as
 *   it now is, or throws an Error whose message can be shown as it is.
 * - The page's frame (OSS `AppScreen`, cloud `OrgScreen`), so the hub's title
 *   and tabs stay each product's own, and the visitor-tools picker, which reads
 *   each product's tool catalogue.
 * - Whether this person may change anything (cloud's viewers may not; OSS has
 *   no roles), and whether calls are charged in credits (cloud).
 *
 * Without a provider the screens get `DEFAULT_PHONE_HOST`: no numbers and
 * nothing to do, never a broken request.
 */
import { createContext, useContext, type ComponentType, type ReactNode } from 'react';

import {
    EMPTY_OPTIONS, type CallRecord, type PhoneAgent, type PhoneNumber, type PhoneOptions, type Transcript,
    type TwilioKey, type TwilioNumber, type VoiceSettings,
} from './phone';

/** Step three's choices, and the key and number from steps one and two. */
export type ConnectInput = {
    key: TwilioKey;
    numberSid: string;
    number: string;
    name: string;
    agentId: string;
    voice: VoiceSettings;
};

/** What one Save changes on a number: only what the person changed is present. */
export type NumberPatch = {
    name?: string;
    agentId?: string;
    visitorTools?: string[];
    voice?: VoiceSettings;
};

export type VisitorToolsProps = {
    /** The answering agent's id: its tools are the ones a caller may be allowed. */
    agentIds: string[];
    ticked: string[];
    onChange: (next: string[]) => void;
    disabled: boolean;
};

export type PhoneHost = {
    numbers: () => Promise<PhoneNumber[]>;
    options: () => Promise<PhoneOptions>;
    agents: () => Promise<PhoneAgent[]>;
    /** A number's recent calls, newest first. */
    calls: (numberId: string) => Promise<CallRecord[]>;
    /** A call's conversation (its chat session), and the question it parked, if any is still open. */
    transcript: (call: CallRecord) => Promise<Transcript>;
    /** Approve or refuse the question a call parked (V16): the turn carries on behind, its result kept with the call. */
    answer: (call: CallRecord, approve: boolean, approvalId: string | null) => Promise<void>;
    /** What the call's model calls cost, in dollars; null when nothing is recorded. Never rejects. */
    modelCost: (call: CallRecord) => Promise<number | null>;
    /** The call's voicemail, to play in its log (F480): read from Twilio with the number's key, never stored. Rejects
     *  with words to show, such as which permission the key lacks. */
    voicemail: (call: CallRecord) => Promise<Blob>;
    /** The numbers a key can see (step two). A refused key rejects with Twilio's reason. */
    twilioNumbers: (key: TwilioKey) => Promise<TwilioNumber[]>;
    /** Connect a number and point it at us. Resolves with it, its notes (`manual_url`, `signing_error`) included. */
    connect: (input: ConnectInput) => Promise<PhoneNumber>;
    save: (number: PhoneNumber, patch: NumberPatch) => Promise<PhoneNumber>;
    /** Turn a number off (its address put back) or on (pointed again); `left` is what Twilio kept. */
    setEnabled: (number: PhoneNumber, enabled: boolean) => Promise<{ number: PhoneNumber | null; left: string[] }>;
    remove: (number: PhoneNumber) => Promise<{ left: string[] }>;
    /** The page's frame: its title, the hub's tabs, and `actions` in the header. */
    Frame: ComponentType<{ actions: ReactNode; children: ReactNode }>;
    /** What a caller may make the agent use (D233's picker). */
    VisitorTools: ComponentType<VisitorToolsProps>;
    canEdit: boolean;
    /** Calls are charged in credits here (cloud: 4 a started minute, V21). */
    credits: boolean;
    links: {
        /** An agent's page, where its model, thinking and planning are set. */
        agent: (agentId: string) => string;
        /** Where the public address is set, for the "can't take calls yet" note (OSS). */
        address?: { label: string; href: string };
    };
    /** Step three's line about what a call costs here. */
    costLine: string;
    /** The longest call when a number sets none (the plan's, or OSS's 30 minutes). */
    longestCall: string;
};

const nothing = async () => { throw new Error('Phone calls are not available here.'); };

export const DEFAULT_PHONE_HOST: PhoneHost = {
    numbers: async () => [],
    options: async () => EMPTY_OPTIONS,
    agents: async () => [],
    calls: async () => [],
    transcript: async () => ({ turns: [], pending: null }),
    answer: nothing,
    modelCost: async () => null,
    voicemail: nothing,
    twilioNumbers: nothing,
    connect: nothing,
    save: nothing,
    setEnabled: nothing,
    remove: nothing,
    Frame: ({ children }) => <>{children}</>,
    VisitorTools: () => null,
    canEdit: false,
    credits: false,
    links: { agent: () => '#' },
    costLine: '',
    longestCall: '',
};

const PhoneHostContext = createContext<PhoneHost>(DEFAULT_PHONE_HOST);

export function PhoneHostProvider({ value, children }: { value: PhoneHost; children: ReactNode }) {
    return <PhoneHostContext.Provider value={value}>{children}</PhoneHostContext.Provider>;
}

export function usePhoneHost(): PhoneHost {
    return useContext(PhoneHostContext);
}
