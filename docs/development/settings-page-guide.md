# Settings page composition guide

Use the shared settings composition for repository-owned configuration and
management pages. It gives pages one header, consistent spacing, explicit
section widths, and stable search metadata without owning business state.

## Imports and responsibilities

```tsx
import { SettingsPage, SettingsSection } from "@/components/settings";
```

`SettingsPage` owns only page structure: one existing `PageHeader`, optional
navigation, toolbar and feedback slots, and the content flow. `SettingsSection`
owns a named region, optional contextual help and actions, width, and explicit
search attributes. Neither component fetches data, registers fields, saves,
navigates, checks permissions, or controls a modal.

Keep values, `Form`, request scope, save order, close confirmation, retries,
and focus restoration in the existing page or hook. Do not add `value`,
`onSave`, `agentId`, or endpoint props to the layout components. Feedback must
come from a real business state; do not infer success from a timer or from
`loading === false`.

## Form composition

```tsx
<SettingsPage header={{ current: t("settings.title") }}>
  <SettingsSection
    id="runtime"
    title={t("settings.runtime")}
    width="readable"
    data-settings-target="runtime"
  >
    <Form form={form} onValuesChange={scheduleSave}>
      <SettingsField name="timeout" label={t("settings.timeout")}>
        <InputNumber />
      </SettingsField>
    </Form>
  </SettingsSection>
</SettingsPage>
```

Use `readable` for linear forms and prose-heavy controls. Keep the original
field names, rules, dependencies, `valuePropName`, and form instance.
`SettingsSection` never replaces `Form.Item` or `SettingsField`.

## Collection composition

Use `full` for cards, tables, filters, and bulk actions. Put page-level filters
in `toolbar` or section actions, and keep creation/editing in the existing
`SettingsDrawer`, modal, or route. Stable entity keys and the existing close
handler must survive layout changes.

When a collection already owns a richer shared header, expose that header's
props and pass them to `SettingsPage` instead of nesting its `PageHeader`.
For example, the marketplace section switch and actions are produced by
`useMarketplaceHeaderProps`; both the standalone market page and plugin
manager consume the same navigation contract while rendering one header.

An embedded settings block must not create another `SettingsPage`. General
settings owns the page header, while its navigation editor is a named
`SettingsSection`. This keeps search targets and sortable IDs stable without
introducing a nested page landmark.

## Data and diagnostic composition

Charts, logs, tables, and dense statistics use `full`. Keep local scrolling
only where the existing table, log viewer, or editor requires it. The host owns
the main vertical scroll; `SettingsPage` must not introduce another viewport
height or `overflow-y: auto`.

## Flow composition

Multi-step imports, restores, and other consequential operations keep their
original controller, confirmation sequence, cancellation entry point, and
target entity. The page skeleton may present steps and feedback, but closing a
surface must not advance or cancel work unless the existing business handler
does so.

## Search, themes, and accessibility

- Pass existing `data-settings-target` and `data-settings-labels` values
  explicitly. Never derive them from translated text or array position.
- Keep `data-settings-open`, tab keys, `aria-controls`, and the actual focusable
  target on the interactive element that owns the behavior.
- Use semantic tokens from `console/src/styles/tokens.css`. Do not hardcode the
  built-in orange, surfaces, text, border, or focus colors.
- Keep essential errors and pending states visible. Use `InlineHelp` only for
  secondary explanations that work by hover, focus, and touch.
- Preserve one page header, logical heading order, keyboard focus, reduced
  motion, and useful touch targets.

## Review checklist

- The page has exactly one `data-page-header` and no second main scroll.
- Feedback updates do not replace the form, editor, or stable entity key.
- Search can open the original hidden surface and focus the original target.
- The same valid interaction produces the same request target, payload,
  trigger, count, and close/navigation result as before migration.
- 375, 768, and 1280 px layouts, 200% zoom, light/dark and custom accents,
  long translations, keyboard/touch, and reduced motion are checked where
  applicable.
- Every changed page has traceable before/after screenshots with matching
  route, viewport, theme, language, permissions, entity, data, and state.

Specialized workspaces such as Chat, Files, Login, and embedded applications
keep their own skeleton. Sharing tokens and controls does not make them
settings forms.
