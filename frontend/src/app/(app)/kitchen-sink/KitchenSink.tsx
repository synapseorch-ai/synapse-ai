"use client";

/**
 * Every primitive, in every state, on one page.
 *
 * Two things this catches that clicking through the product does not. First,
 * states nobody navigates to on purpose: loading, disabled, invalid, the
 * indeterminate checkbox, a badge with a word long enough to wrap. Second,
 * *light mode* — `globals.css` has carried a full `.light-mode`
 * palette since the beginning with nothing in the app to switch it on, so
 * until now no one had ever seen it. The toggle at the top sets that attribute.
 *
 * Reachability is decided by `page.tsx` and the middleware, both server-side.
 * It cannot be decided here: a client component only sees `NEXT_PUBLIC_*` and
 * `NODE_ENV`, and the dev stack runs `NODE_ENV=production` inside Docker — so
 * gating on that would hide this page in the one environment it is for.
 */

import { Bot, Copy, ExternalLink, Pencil, Plus, Trash2 } from "lucide-react";
import * as React from "react";

import {
  Badge,
  Button,
  Card,
  CardBody,
  CardFooter,
  CellStack,
  Checkbox,
  ConfirmDialog,
  EmptyState,
  ErrorNote,
  Field,
  Heading,
  Label,
  Hint,
  IconButton,
  Input,
  LinkButton,
  MenuItem,
  MenuSeparator,
  Modal,
  Mono,
  Muted,
  PageHeader,
  RowMenu,
  SearchInput,
  Section,
  Select,
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
  Text,
  Textarea,
  TextLink,
  Toast,
  type ToastTone,
  TooltipProvider,
  type BadgeTone,
  type ButtonVariant,
  type Density,
} from "@/components/ui";

const VARIANTS: ButtonVariant[] = ["primary", "secondary", "danger", "ghost"];
const TONES: BadgeTone[] = ["neutral", "accent", "danger", "warning", "success"];
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
  const [theme, setTheme] = React.useState<"dark" | "light">("dark");
  const [text, setText] = React.useState("");
  const [query, setQuery] = React.useState("");
  const [checked, setChecked] = React.useState(true);
  const [selectValue, setSelectValue] = React.useState("conversational");
  const [density, setDensity] = React.useState<Density>("comfortable");
  const [modal, setModal] = React.useState(false);
  const [confirm, setConfirm] = React.useState(false);
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
    if (wanted === "light" || wanted === "dark") setTheme(wanted);
  }, []);

  React.useEffect(() => {
    // The palette is keyed off the root element, exactly as a real theme
    // switcher would set it. `data-theme-switching` suppresses every
    // `transition-colors` for the duration of the swap — without it the
    // controls animate between palettes while the cards, which have no
    // transition, change instantly, and the page spends a beat in neither
    // theme.
    const root = document.documentElement;
    root.setAttribute("data-theme-switching", "");
    // This app keys light mode off a class, not `data-theme` — see the shell
    // layout, which owns the same flag in localStorage.
    root.classList.toggle("light-mode", theme === "light");
    window.localStorage.setItem("synapseTheme", theme);
    window.dispatchEvent(new Event("synapse-local-storage"));
    const frame = requestAnimationFrame(() =>
      requestAnimationFrame(() => root.removeAttribute("data-theme-switching")),
    );
    return () => {
      cancelAnimationFrame(frame);
      root.removeAttribute("data-theme");
      root.removeAttribute("data-theme-switching");
    };
  }, [theme]);

  return (
    <TooltipProvider>
      {/* The shell's <main> is `overflow-hidden` and every other screen brings
          its own scroller (the kit's `Screen`). This page does not use `Screen`
          — it is demonstrating the primitives, header included — so it has to
          own the scroller itself, or 3,498px of it is simply unreachable. */}
      <div className="h-full overflow-y-auto">
        <div className="mx-auto w-full max-w-[88rem] space-y-8 px-8 py-7">
        <PageHeader
          title="Kitchen sink"
          description="Every primitive in the design system, in the states you cannot reach by clicking around the product."
          actions={
            <Button
              variant="secondary"
              onClick={() => setTheme((t) => (t === "dark" ? "light" : "dark"))}
            >
              Switch to {theme === "dark" ? "light" : "dark"}
            </Button>
          }
        />

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
                  { value: "delegate", label: "Delegate", disabled: true },
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
              {(["success", "warning", "danger"] as const).map((tone) => (
                <Button
                  key={tone}
                  variant={tone === "danger" ? "danger" : "secondary"}
                  onClick={() =>
                    setToast({
                      message: {
                        success: "Saved.",
                        warning: "Config saved. Use Retry to reconnect.",
                        danger: "Could not reach the server.",
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
        </div>

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
        </Modal>

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
        </div>
      </div>
    </TooltipProvider>
  );
}
