import { Shield } from 'lucide-react';

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

export const PersonalDetailsTab = ({
    pdFirstName, setPdFirstName, pdLastName, setPdLastName,
    pdEmail, setPdEmail, pdPhone, setPdPhone,
    pdAddress1, setPdAddress1, pdAddress2, setPdAddress2,
    pdCity, setPdCity, pdState, setPdState,
    pdZipcode, setPdZipcode, onSave
}: PersonalDetailsTabProps) => (
    <div className="space-y-8">
        <div className="mb-4">
            <h3 className="text-lg font-bold text-white flex items-center gap-2">
                <Shield className="h-5 w-5" />
                Personal Details
            </h3>
            <p className="text-text-faint text-sm mt-1">
                Saved details the agent can use when completing workflows.
            </p>
        </div>

        <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
            <div className="space-y-2">
                <label htmlFor="personaldetails-first-name" className="text-xs uppercase font-bold text-text-faint tracking-wider">First Name</label>
                <input
                    id="personaldetails-first-name"
                    type="text"
                    value={pdFirstName}
                    onChange={(e) => setPdFirstName(e.target.value)}
                    className="w-full bg-zinc-900 border border-zinc-800 p-3 text-sm text-white focus:border-white focus:outline-none transition-colors rounded-md"
                    placeholder="First name"
                />
            </div>
            <div className="space-y-2">
                <label htmlFor="personaldetails-last-name" className="text-xs uppercase font-bold text-text-faint tracking-wider">Last Name</label>
                <input
                    id="personaldetails-last-name"
                    type="text"
                    value={pdLastName}
                    onChange={(e) => setPdLastName(e.target.value)}
                    className="w-full bg-zinc-900 border border-zinc-800 p-3 text-sm text-white focus:border-white focus:outline-none transition-colors rounded-md"
                    placeholder="Last name"
                />
            </div>
            <div className="space-y-2">
                <label htmlFor="personaldetails-email" className="text-xs uppercase font-bold text-text-faint tracking-wider">Email</label>
                <input
                    id="personaldetails-email"
                    type="email"
                    value={pdEmail}
                    onChange={(e) => setPdEmail(e.target.value)}
                    className="w-full bg-zinc-900 border border-zinc-800 p-3 text-sm text-white focus:border-white focus:outline-none transition-colors font-mono rounded-md"
                    placeholder="name@company.com"
                />
            </div>
            <div className="space-y-2">
                <label htmlFor="personaldetails-phone-number" className="text-xs uppercase font-bold text-text-faint tracking-wider">Phone Number</label>
                <input
                    id="personaldetails-phone-number"
                    type="tel"
                    value={pdPhone}
                    onChange={(e) => setPdPhone(e.target.value)}
                    className="w-full bg-zinc-900 border border-zinc-800 p-3 text-sm text-white focus:border-white focus:outline-none transition-colors font-mono rounded-md"
                    placeholder="+1 555 555 5555"
                />
            </div>
        </div>

        <div className="border border-zinc-800 bg-zinc-900/20 p-6 space-y-6 rounded-md">
            <div>
                <div className="text-sm font-bold text-white">Address</div>
                <div className="text-xs text-text-faint">Used when a workflow needs a billing or mailing address.</div>
            </div>

            <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
                <div className="space-y-2">
                    <label htmlFor="personaldetails-address-1" className="text-xs uppercase font-bold text-text-faint tracking-wider">Address 1</label>
                    <input
                        id="personaldetails-address-1"
                        type="text"
                        value={pdAddress1}
                        onChange={(e) => setPdAddress1(e.target.value)}
                        className="w-full bg-zinc-900 border border-zinc-800 p-3 text-sm text-white focus:border-white focus:outline-none transition-colors rounded-md"
                        placeholder="Street address"
                    />
                </div>
                <div className="space-y-2">
                    <label htmlFor="personaldetails-address-2" className="text-xs uppercase font-bold text-text-faint tracking-wider">Address 2</label>
                    <input
                        id="personaldetails-address-2"
                        type="text"
                        value={pdAddress2}
                        onChange={(e) => setPdAddress2(e.target.value)}
                        className="w-full bg-zinc-900 border border-zinc-800 p-3 text-sm text-white focus:border-white focus:outline-none transition-colors rounded-md"
                        placeholder="Apt, suite, unit"
                    />
                </div>
                <div className="space-y-2">
                    <label htmlFor="personaldetails-city" className="text-xs uppercase font-bold text-text-faint tracking-wider">City</label>
                    <input
                        id="personaldetails-city"
                        type="text"
                        value={pdCity}
                        onChange={(e) => setPdCity(e.target.value)}
                        className="w-full bg-zinc-900 border border-zinc-800 p-3 text-sm text-white focus:border-white focus:outline-none transition-colors rounded-md"
                        placeholder="City"
                    />
                </div>
                <div className="space-y-2">
                    <label htmlFor="personaldetails-state" className="text-xs uppercase font-bold text-text-faint tracking-wider">State</label>
                    <input
                        id="personaldetails-state"
                        type="text"
                        value={pdState}
                        onChange={(e) => setPdState(e.target.value)}
                        className="w-full bg-zinc-900 border border-zinc-800 p-3 text-sm text-white focus:border-white focus:outline-none transition-colors rounded-md"
                        placeholder="State"
                    />
                </div>
                <div className="space-y-2">
                    <label htmlFor="personaldetails-zipcode" className="text-xs uppercase font-bold text-text-faint tracking-wider">Zipcode</label>
                    <input
                        id="personaldetails-zipcode"
                        type="text"
                        value={pdZipcode}
                        onChange={(e) => setPdZipcode(e.target.value)}
                        className="w-full bg-zinc-900 border border-zinc-800 p-3 text-sm text-white focus:border-white focus:outline-none transition-colors font-mono rounded-md"
                        placeholder="Zipcode"
                    />
                </div>
            </div>
        </div>

        <div className="pt-2 flex justify-end">
            <button
                onClick={onSave}
                className="px-6 py-2.5 text-sm font-bold bg-accent text-accent-fg hover:bg-accent-hover transition-all shadow-lg"
            >
                Save Changes
            </button>
        </div>
    </div>
);
