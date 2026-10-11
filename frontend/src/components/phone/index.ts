/*
 * The Phone tab (VOX1c): a Twilio number an agent answers. Synced byte for byte
 * into the cloud app; each product provides a `PhoneHost` (see `host.tsx`).
 */
export { AddNumber } from './AddNumber';
export { CallLog, OutcomeWord } from './CallLog';
export {
    DEFAULT_PHONE_HOST, PhoneHostProvider, usePhoneHost,
    type ConnectInput, type NumberPatch, type PhoneHost, type VisitorToolsProps,
} from './host';
export { NumberPane } from './NumberPane';
export { PhoneScreen, type PhoneInitial } from './PhoneScreen';
export * from './phone';
