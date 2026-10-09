import { Input, Section } from '@/components/ui';
import { FORM } from './form';
import { SaveBar } from './SaveBar';

interface PersonalDetailsTabProps {
    pdFirstName: string; setPdFirstName: (v: string) => void;
    pdLastName: string; setPdLastName: (v: string) => void;
    pdEmail: string; setPdEmail: (v: string) => void;
    pdPhone: string; setPdPhone: (v: string) => void;
    pdAddress1: string; setPdAddress1: (v: string) => void;
    pdAddress2: string; setPdAddress2: (v: string) => void;
    pdCity: string; setPdCity: (v: string) => void;
    pdState: string; setPdState: (v: string) => void;
    pdZipcode: string; setPdZipcode: (v: string) => void;
    onSave: () => void;
}

/** One text field: the kit's label above its input, the id kept so links and checks still reach it. */
function TextField({ id, label, value, onChange, placeholder, type = 'text', mono }: {
    id: string; label: string; value: string; onChange: (v: string) => void; placeholder: string;
    type?: string; mono?: boolean;
}) {
    return (
        <div className="space-y-1.5">
            <label htmlFor={id} className="block text-xs font-medium text-text-muted">{label}</label>
            <Input id={id} type={type} value={value} onChange={e => onChange(e.target.value)} placeholder={placeholder}
                className={mono ? 'font-code' : undefined} />
        </div>
    );
}

// The page's title already says "Personal details"; its sentence leads the form
// instead of a second heading (M2f's rule), and Address is the one group (M2g).
export const PersonalDetailsTab = ({
    pdFirstName, setPdFirstName, pdLastName, setPdLastName,
    pdEmail, setPdEmail, pdPhone, setPdPhone,
    pdAddress1, setPdAddress1, pdAddress2, setPdAddress2,
    pdCity, setPdCity, pdState, setPdState,
    pdZipcode, setPdZipcode, onSave
}: PersonalDetailsTabProps) => (
    <div>
        <div className={`${FORM} space-y-5 pb-6`}>
            <p className="text-sm text-text-muted">Saved details the agent can use when completing workflows.</p>
            <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
                <TextField id="personaldetails-first-name" label="First name" value={pdFirstName} onChange={setPdFirstName} placeholder="First name" />
                <TextField id="personaldetails-last-name" label="Last name" value={pdLastName} onChange={setPdLastName} placeholder="Last name" />
                <TextField id="personaldetails-email" label="Email" type="email" value={pdEmail} onChange={setPdEmail} placeholder="name@company.com" />
                <TextField id="personaldetails-phone-number" label="Phone number" type="tel" value={pdPhone} onChange={setPdPhone} placeholder="+1 555 555 5555" />
            </div>
        </div>

        <Section variant="plain" collapsible={false} title="Address" description="Used when a workflow needs a billing or mailing address.">
            <div className={`grid ${FORM} grid-cols-1 gap-4 md:grid-cols-2`}>
                <TextField id="personaldetails-address-1" label="Address 1" value={pdAddress1} onChange={setPdAddress1} placeholder="Street address" />
                <TextField id="personaldetails-address-2" label="Address 2" value={pdAddress2} onChange={setPdAddress2} placeholder="Apt, suite, unit" />
                <TextField id="personaldetails-city" label="City" value={pdCity} onChange={setPdCity} placeholder="City" />
                <TextField id="personaldetails-state" label="State" value={pdState} onChange={setPdState} placeholder="State" />
                <TextField id="personaldetails-zipcode" label="Zipcode" value={pdZipcode} onChange={setPdZipcode} placeholder="Zipcode" />
            </div>
        </Section>

        <SaveBar onSave={onSave} />
    </div>
);
