"use client";

/**
 * Every primitive, in every state, on one page.
 *
 * Two things this catches that clicking through the product does not. First,
 * states nobody navigates to on purpose: loading, disabled, invalid, the
 * indeterminate checkbox, a badge with a word long enough to wrap. Second,
 * both themes — the toggle at the top switches the app's own theme (the
 * shell's `data-theme`, through `lib/theme.ts`), and `?theme=light|dark` opens
 * straight into one, which is what makes each reviewable in a screenshot.
 *
 * Reachability is decided by `page.tsx` and the middleware, both server-side.
 * It cannot be decided here: a client component only sees `NEXT_PUBLIC_*` and
 * `NODE_ENV`, and the dev stack runs `NODE_ENV=production` inside Docker — so
 * gating on that would hide this page in the one environment it is for.
 */

import { Bot, Copy, ExternalLink, LogOut, Monitor, Moon, Pencil, Plus, Settings, Sun, Trash2 } from "lucide-react";
import * as React from "react";

import { parseThemeMode, switchTheme, THEME_KEY, useAppliedTheme, type ThemeMode } from "@/lib/theme";

import {
  Badge,
  Button,
  Card,
  CardBody,
  CardFooter,
  CellStack,
  Checkbox,
  Switch,
  Combobox,
  ConfirmDialog,
  DataTable,
  Drawer,
  EmptyState,
  ErrorNote,
  Field,
  Heading,
  Label,
  Hint,
  Mark,
  Menu,
  MenuLabel,
  MenuLink,
  MenuSegment,
  IconButton,
  Input,
  LinkButton,
  MenuItem,
  MenuSeparator,
  Modal,
  Mono,
  Muted,
  PageFrame,
  RowMenu,
  SearchInput,
  Section,
  sectionClass,
  Select,
  Spinner,
  Stat,
  StatusBadge,
  SuccessNote,
  TBody,
  TD,
  TH,
  THead,
  TR,
  Table,
  TableActions,
  Tag,
  TemplateInput,
  TemplateScope,
  Text,
  Textarea,
  TextLink,
  titleClass,
  Toast,
  type ToastTone,
  TooltipProvider,
  usePersisted,
  type BadgeTone,
  type ButtonVariant,
  type Density,
} from "@/components/ui";

const VARIANTS: ButtonVariant[] = ["primary", "secondary", "danger", "ghost", "person"];
const TONES: BadgeTone[] = ["neutral", "accent", "machine", "person", "danger", "warning", "success"];
const MODEL_OPTIONS = [
  { value: "nova-pro", label: "Nova Pro", hint: "Managed", group: "Synapse" },
  { value: "nova-micro", label: "Nova Micro", hint: "Managed", group: "Synapse" },
  { value: "gpt-5-mini", label: "GPT-5 mini", group: "OpenAI" },
  { value: "gemini-flash", label: "Gemini Flash", group: "Google" },
];
/**
 * Sixty of each kind of dropdown, inside the modal and the drawer below. A list
 * opened in a dialog must still scroll under the wheel and a finger, and a long
 * menu must scroll rather than run off the screen (F700, F701; ui-smoke's
 * `dropdownscroll` checks each one).
 */
const LONG_OPTIONS = Array.from({ length: 60 }, (_, i) => {
  const n = String(i + 1).padStart(2, "0");
  return { value: `option-${n}`, label: `Option ${n}` };
});
const LONG_STATE = LONG_OPTIONS.map((o) => o.value.replace("-", "_"));

function LongLists() {
  const [pick, setPick] = React.useState<string | undefined>();
  const [choice, setChoice] = React.useState("option-01");
  const [prompt, setPrompt] = React.useState("");
  return (
    <div className="mt-4 grid gap-4 sm:grid-cols-2">
      <Field label="Long combobox">
        <Combobox value={pick} onChange={setPick} options={LONG_OPTIONS} aria-label="Long combobox" />
      </Field>
      <Field label="Long select">
        <Select value={choice} onChange={setChoice} options={LONG_OPTIONS} aria-label="Long select" />
      </Field>
      <Field label="Long template">
        <TemplateScope state={LONG_STATE}>
          <TemplateInput
            profile="step-prompt"
            value={prompt}
            onChange={(e) => setPrompt(e.target.value)}
            aria-label="Long template"
            placeholder="Type {{ for suggestions"
          />
        </TemplateScope>
      </Field>
      <div className="self-end">
        <Menu trigger={<Button variant="secondary">Long menu</Button>}>
          {LONG_OPTIONS.map((o) => (
            <MenuItem key={o.value} onSelect={() => {}}>
              {o.label}
            </MenuItem>
          ))}
        </Menu>
      </div>
    </div>
  );
}

type Sample = { id: string; name: string; status: string; runs: number };
const SAMPLE_ROWS: Sample[] = [
  { id: "a", name: "Order desk", status: "completed", runs: 128 },
  { id: "b", name: "Supplier review", status: "running", runs: 12 },
  { id: "c", name: "Invoice triage", status: "paused", runs: 3 },
  { id: "d", name: "Weekly digest", status: "failed", runs: 41 },
];
const STATUSES = ["queued", "running", "paused", "completed", "failed", "cancelled"];

function Row({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="space-y-2">
      <Heading level={3}>{title}</Heading>
      <div className="flex flex-wrap items-center gap-3">{children}</div>
    </div>
  );
}

export function KitchenSink() {
  // The app's own theme, not a copy of it: the toggle and `?theme=` go through
  // the shell's setting, and what is shown is what the shell applied. (A copy
  // kept here once wrote "dark" on every visit, so a light pass measured this
  // page dark, and its cleanup removed the attribute the shell had set.)
  const [, setMode] = usePersisted<ThemeMode>(THEME_KEY, parseThemeMode, "system");
  const theme = useAppliedTheme();
  const [text, setText] = React.useState("");
  const [query, setQuery] = React.useState("");
  const [checked, setChecked] = React.useState(true);
  const [selectValue, setSelectValue] = React.useState("conversational");
  const [model, setModel] = React.useState<string | undefined>("nova-pro");
  const [segment, setSegment] = React.useState<"system" | "light" | "dark">("system");
  const [picked, setPicked] = React.useState<string[]>([]);
  const [density, setDensity] = React.useState<Density>("comfortable");
  const [modal, setModal] = React.useState(false);
  const [confirm, setConfirm] = React.useState(false);
  const [drawer, setDrawer] = React.useState(false);
  const [toast, setToast] = React.useState<{ message: string; tone: ToastTone }>({
    message: "",
    tone: "success",
  });

  // `?theme=light` opens straight into the other palette, which is what makes
  // it reviewable in a screenshot rather than only by clicking. Read in an
  // effect rather than during render: the server has no query string in a
  // statically-rendered page, and seeding state from one would hydrate a
  // different tree than it sent.
  React.useEffect(() => {
    const wanted = new URLSearchParams(window.location.search).get("theme");
    if (wanted === "light" || wanted === "dark") switchTheme(setMode, wanted);
  }, [setMode]);

  return (
    <TooltipProvider>
      {/* The page frame, like every other screen (M1a): it brings the
          scroller the shell's `overflow-hidden` <main> needs, and the title sits
          where every other page's does. */}
      <PageFrame
        eyebrow="Design system"
        title="Kitchen sink"
        description="Every primitive in the design system, in the states you cannot reach by clicking around the product."
        width="wide"
        actions={
          <Button
            variant="secondary"
            onClick={() => switchTheme(setMode, theme === "dark" ? "light" : "dark")}
          >
            Switch to {theme === "dark" ? "light" : "dark"}
          </Button>
        }
      >

        <Card title="Buttons" description="Hover each one: the cursor must become a pointer.">
          <CardBody className="space-y-5">
            {(["md", "sm"] as const).map((size) => (
              <Row key={size} title={size === "md" ? "Default size" : "Small"}>
                {VARIANTS.map((variant) => (
                  <Button key={variant} variant={variant} size={size}>
                    {variant}
                  </Button>
                ))}
                <Button size={size} loading>
                  loading
                </Button>
                <Button size={size} disabled>
                  disabled
                </Button>
              </Row>
            ))}

            <Row title="Icon buttons">
              <IconButton label="Edit" icon={Pencil} />
              <IconButton label="Copy" icon={Copy} />
              <IconButton label="Delete" icon={Trash2} tone="danger" />
              <IconButton label="Edit, small" icon={Pencil} size="sm" />
              <Hint content="Disabled controls still explain themselves — that is what the wrapper is for.">
                <IconButton label="Unavailable" icon={Trash2} disabled />
              </Hint>
              <RowMenu>
                <MenuItem icon={Pencil} onSelect={() => {}}>
                  Edit
                </MenuItem>
                <MenuItem icon={ExternalLink} onSelect={() => {}}>
                  Open
                </MenuItem>
                <MenuSeparator />
                <MenuItem icon={Trash2} tone="danger" onSelect={() => {}}>
                  Delete
                </MenuItem>
              </RowMenu>
            </Row>

            <Row title="Links, and links shaped like buttons">
              <TextLink href="/kitchen-sink">Accent link</TextLink>
              <TextLink href="/kitchen-sink" variant="quiet">
                Quiet link
              </TextLink>
              <TextLink href="/kitchen-sink" variant="plain">
                Plain link
              </TextLink>
              <LinkButton href="/kitchen-sink">
                <Plus className="size-4" aria-hidden />
                Link button
              </LinkButton>
              <LinkButton href="/kitchen-sink" variant="primary">
                Primary link button
              </LinkButton>
            </Row>
          </CardBody>
        </Card>

        <Card title="Form controls" description="A select and an input in one row must be the same height.">
          <CardBody className="grid max-w-3xl gap-4 sm:grid-cols-2">
            <Field label="Name" required hint="A hint is announced with the control, not just near it.">
              <Input value={text} onChange={(e) => setText(e.target.value)} placeholder="Researcher" />
            </Field>
            <Field label="Type">
              <Select
                value={selectValue}
                onChange={setSelectValue}
                aria-label="Type"
                options={[
                  { value: "conversational", label: "Conversational", hint: "Multi-turn chat." },
                  { value: "analysis", label: "Analysis", hint: "One-shot reasoning." },
                  { value: "delegate", label: "Team", disabled: true },
                ]}
              />
            </Field>
            <Field label="Invalid" error="That address is already in use.">
              <Input defaultValue="taken@example.com" />
            </Field>
            <Field label="Disabled">
              <Input value="acme.synapseorch.com" disabled readOnly />
            </Field>
            <Field label="System prompt" className="sm:col-span-2">
              <Textarea rows={3} placeholder="You are a careful researcher." />
            </Field>
            <div className="space-y-3 sm:col-span-2">
              <SearchInput value={query} onChange={setQuery} placeholder="Search agents…" />
              <div className="flex flex-wrap items-center gap-4">
                <Checkbox checked={checked} onChange={setChecked} label="Checked" />
                <Checkbox checked="indeterminate" onChange={() => {}} label="Indeterminate" />
                <Checkbox checked={false} onChange={() => {}} label="Unchecked" />
                <Checkbox checked={false} onChange={() => {}} label="Disabled" disabled />
                <Switch checked={checked} onChange={setChecked} label="Switch" />
                <Switch checked={false} onChange={() => {}} label="Switch off" />
                <Switch checked onChange={() => {}} label="Switch small" size="sm" />
                <Switch checked={false} onChange={() => {}} label="Switch disabled" disabled />
              </div>
            </div>
          </CardBody>
          <CardFooter>
            <Muted>Footers hold what a form does next.</Muted>
            {/* wrap: five buttons in a row are wider than a phone, and a row
                that cannot wrap takes the whole document sideways with it */}
            <div className="flex flex-wrap gap-2">
              <Button variant="secondary" onClick={() => setModal(true)}>
                Open modal
              </Button>
              <Button variant="danger" onClick={() => setConfirm(true)}>
                Delete something
              </Button>
              <Button variant="secondary" onClick={() => setDrawer(true)}>
                Open drawer
              </Button>
              {(["success", "warning", "danger", "person"] as const).map((tone) => (
                <Button
                  key={tone}
                  variant={tone === "danger" ? "danger" : "secondary"}
                  onClick={() =>
                    setToast({
                      message: {
                        success: "Saved.",
                        warning: "Config saved. Use Retry to reconnect.",
                        danger: "Could not reach the server.",
                        person: "Invoice triage needs your approval",
                      }[tone],
                      tone,
                    })
                  }
                >
                  Toast: {tone}
                </Button>
              ))}
            </div>
          </CardFooter>
        </Card>

        <Card title="Type and tone">
          <CardBody className="space-y-5">
            <Row title="Badges">
              {TONES.map((tone) => (
                <Badge key={tone} tone={tone}>
                  {tone}
                </Badge>
              ))}
              {TONES.map((tone) => (
                <Badge key={`${tone}-dot`} tone={tone} dot>
                  {tone}
                </Badge>
              ))}
              <Badge size="sm">small</Badge>
              <Tag>plain tag</Tag>
              <Tag onRemove={() => {}}>removable</Tag>
            </Row>
            <Row title="Run status">
              {STATUSES.map((status) => (
                <StatusBadge key={status} status={status} />
              ))}
              <StatusBadge status="running" waitingForHuman />
              <StatusBadge status={null} />
            </Row>
            <div className="space-y-2">
              <Heading level={3}>Text</Heading>
              <Text>Default body text, at the size the product uses for prose.</Text>
              <Text tone="muted">Muted: supporting detail under a heading.</Text>
              <Text tone="faint">Faint: the quietest thing on the page.</Text>
              <p>
                <Mono>run_01j9x2c8ab</Mono> <Muted>· ids and keys are monospaced</Muted>
              </p>
              <div className="space-y-1 border-t border-border pt-3">
                <Label size="sm">Section eyebrow · sm</Label>
                <Label>Dense panel label · xs</Label>
                <Muted>
                  Both sizes are 1 element. Not for naming a control — that is `Field`.
                </Muted>
              </div>
            </div>
          </CardBody>
        </Card>

        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
          <Stat label="Neutral" value="128" icon={Bot} />
          <Stat label="Accent" value="12" tone="accent" hint="With a hint" icon={Bot} />
          <Stat label="Warning" value="3" tone="warning" icon={Bot} />
          <Stat label="Small" value="4m 20s" size="sm" />
          <Stat label="Machine" value="2 running" tone="machine" icon={Bot} />
          <Stat label="Person" value="1 waiting" tone="person" icon={Bot} />
          <Stat label="Success" value="98%" tone="success" icon={Bot} />
          <Stat label="Danger" value="1 failed" tone="danger" icon={Bot} />
        </div>

        <Card title="Title scale, the mark, and what floats" description="The title classes are for the page frame (M1a); every overlay sits on the surface with the elevated shadow.">
          <CardBody className="space-y-5">
            <div className="space-y-1">
              <p className={titleClass}>A page title, 28px</p>
              <p className={sectionClass}>A section head, 15px</p>
              <Text tone="muted">Body copy under them, at the product&apos;s reading size.</Text>
            </div>
            <Row title="The mark is the action colour; the loader, where the product works, the machine's (D205)">
              <span className="text-accent"><Mark size={20} compact={false} /></span>
              <span className="text-accent"><Mark size={40} compact={false} /></span>
              <Spinner size={16} className="text-machine" />
              <Spinner size={32} className="text-machine" label="Working" />
            </Row>
            <Row title="Combobox and menu">
              <Combobox
                value={model}
                onChange={setModel}
                options={MODEL_OPTIONS}
                aria-label="Model"
                className="w-64"
              />
              <Menu trigger={<Button variant="secondary">Open menu</Button>}>
                <MenuLabel>Account</MenuLabel>
                <MenuLink href="/kitchen-sink" icon={Settings} hint="⌘,">Settings</MenuLink>
                <MenuSegment
                  label="Theme"
                  value={segment}
                  onChange={setSegment}
                  options={[
                    { value: "system", label: "System theme", icon: Monitor },
                    { value: "light", label: "Light theme", icon: Sun },
                    { value: "dark", label: "Dark theme", icon: Moon },
                  ]}
                />
                <MenuItem icon={LogOut} onSelect={() => {}}>Sign out</MenuItem>
              </Menu>
            </Row>
          </CardBody>
        </Card>

        <Card title="A plain card" variant="plain" description="No border and no ground: a group of content on the page, not a box inside a box.">
          <CardBody>
            <Text tone="muted">A card&apos;s border is for a separate object. This one only groups.</Text>
          </CardBody>
        </Card>

        <Card title="Data table" description="Search, sort, select; header and hover on the neutrals.">
          <DataTable
            rows={SAMPLE_ROWS}
            getRowId={(row) => row.id}
            searchKeys={(row) => [row.name]}
            selection={{ selected: picked, onChange: setPicked }}
            bulkActions={() => <Button size="sm" variant="secondary">Archive</Button>}
            minWidth="32rem"
            columns={[
              { id: "name", header: "Name", cell: (row) => row.name, sortBy: (row) => row.name, width: "full" },
              { id: "status", header: "Last run", cell: (row) => <StatusBadge status={row.status} />, width: "min" },
              { id: "runs", header: "Runs", cell: (row) => row.runs, sortBy: (row) => row.runs, align: "right", width: "min" },
            ]}
          />
        </Card>

        <Card
          title="Table"
          description="The action column hugs its row rather than the window edge, at either density."
          actions={
            <Button
              variant="secondary"
              size="sm"
              onClick={() =>
                setDensity((d) => (d === "comfortable" ? "compact" : "comfortable"))
              }
            >
              {density}
            </Button>
          }
        >
          <Table density={density} minWidth="42rem">
            <THead>
              <TR hover={false}>
                <TH width="full">Orchestration</TH>
                <TH width="min">Status</TH>
                <TH width="min" align="right">
                  Cost
                </TH>
                <TH width="min">
                  <span className="sr-only">Actions</span>
                </TH>
              </TR>
            </THead>
            <TBody>
              {[
                { name: "Nightly report", step: "step_b", status: "running", cost: "$0.0431" },
                { name: "Invoice triage", step: "step_a", status: "paused", cost: "$1.2200" },
                { name: "Lead enrichment", step: "step_f", status: "failed", cost: "$0.0004" },
              ].map((row) => (
                <TR key={row.name}>
                  <TD>
                    <CellStack primary={row.name} secondary={`step ${row.step}`} />
                  </TD>
                  <TD>
                    <StatusBadge status={row.status} />
                  </TD>
                  <TD align="right" nowrap className="tabular-nums text-text-muted">
                    {row.cost}
                  </TD>
                  <TD>
                    <TableActions>
                      <TextLink href="/kitchen-sink">Respond</TextLink>
                      <RowMenu>
                        <MenuItem icon={Copy} onSelect={() => {}}>
                          Copy ID
                        </MenuItem>
                      </RowMenu>
                    </TableActions>
                  </TD>
                </TR>
              ))}
            </TBody>
          </Table>
        </Card>

        <div className="grid gap-4 lg:grid-cols-2">
          <Card title="Notes">
            <CardBody className="space-y-3">
              <ErrorNote>That key has already been revoked.</ErrorNote>
              <SuccessNote>Organisation name updated.</SuccessNote>
            </CardBody>
          </Card>
          <Card title="Empty state">
            <EmptyState
              title="No agents yet"
              action={<LinkButton href="/kitchen-sink">Create one</LinkButton>}
            >
              Create one, then drop it onto a step in the orchestration builder.
            </EmptyState>
          </Card>
        </div>

        <Section
          title="Collapsible section"
          description="Closed content is out of the tab order, not just hidden."
          summary="3 keys"
        >
          <Text tone="muted">Whatever the panel holds.</Text>
        </Section>

        <Modal
          open={modal}
          onClose={() => setModal(false)}
          title="A modal"
          description="Focus is trapped, the page behind stops scrolling, Escape closes."
          footer={
            <>
              <Button variant="secondary" onClick={() => setModal(false)}>
                Cancel
              </Button>
              <Button onClick={() => setModal(false)}>Save</Button>
            </>
          }
        >
          <Field label="Name">
            <Input placeholder="Researcher" />
          </Field>
          <LongLists />
        </Modal>

        <Drawer
          open={drawer}
          onClose={() => setDrawer(false)}
          title="A drawer"
          description="Slides in from the right; focus goes back to whatever opened it."
          footer={<Button onClick={() => setDrawer(false)}>Done</Button>}
        >
          <Field label="Note">
            <Input placeholder="Anything" />
          </Field>
          <LongLists />
        </Drawer>

        <ConfirmDialog
          open={confirm}
          onClose={() => setConfirm(false)}
          onConfirm={() => setConfirm(false)}
          title="Delete 3 agents?"
          description="This cannot be undone."
        />

        <Toast
          message={toast.message}
          tone={toast.tone}
          onDismiss={() => setToast((t) => ({ ...t, message: "" }))}
        />
      </PageFrame>
    </TooltipProvider>
  );
}
