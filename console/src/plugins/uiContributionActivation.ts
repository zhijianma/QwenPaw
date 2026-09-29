/** Host-owned activation boundary for declared UI Contribution slots. */

import { slotRegistry } from "./registry/store";
import type {
  Disposable,
  SlotKind,
  SlotName,
  SlotOpts,
  SlotRenderer,
} from "./registry/types";

export interface UiContributionDeclaration {
  id: string;
  slot: string;
  entrypoint: string;
}

interface StagedRegistration {
  kind: SlotKind;
  name: SlotName;
  render: SlotRenderer;
  opts: SlotOpts;
  deferred: DeferredDisposable;
}

class DeferredDisposable implements Disposable {
  private disposed = false;
  private live?: Disposable;

  bind(live: Disposable): void {
    if (this.disposed) live.dispose();
    else this.live = live;
  }

  isDisposed(): boolean {
    return this.disposed;
  }

  dispose(): void {
    if (this.disposed) return;
    this.disposed = true;
    this.live?.dispose();
  }
}

const declaredSlots = new Map<string, ReadonlySet<string>>();
let active: UiContributionActivation | undefined;

export class UiContributionActivation {
  private readonly allowedSlots: ReadonlySet<string>;
  private readonly staged: StagedRegistration[] = [];
  private closed = false;

  constructor(
    readonly pluginId: string,
    declarations: readonly UiContributionDeclaration[],
  ) {
    if (active) {
      throw new Error(
        `UI Contribution activation already running for '${active.pluginId}'`,
      );
    }
    this.allowedSlots = new Set(declarations.map((item) => item.slot));
    declaredSlots.set(this.pluginId, this.allowedSlots);
    // The host permits one synchronous UI activation transaction at a time.
    // eslint-disable-next-line @typescript-eslint/no-this-alias
    active = this;
  }

  stage(
    pluginId: string,
    kind: SlotKind,
    name: SlotName,
    render: SlotRenderer,
    opts: SlotOpts = {},
  ): Disposable {
    this.assertOpen();
    this.assertRegistration(pluginId, name);
    const deferred = new DeferredDisposable();
    this.staged.push({ kind, name, render, opts, deferred });
    return deferred;
  }

  commit(): void {
    this.assertOpen();
    const registered = new Set(
      this.staged
        .filter((item) => !item.deferred.isDisposed())
        .map((item) => item.name),
    );
    const missing = [...this.allowedSlots].filter(
      (slot) => !registered.has(slot),
    );
    if (missing.length > 0) {
      throw new Error(
        `Plugin '${this.pluginId}' did not register declared UI slots: ` +
          missing.join(", "),
      );
    }

    slotRegistry.removeBySource(this.pluginId);
    for (const item of this.staged) {
      if (item.deferred.isDisposed()) continue;
      const live =
        item.kind === "fill"
          ? slotRegistry.fill(
              this.pluginId,
              item.name,
              item.render,
              item.opts,
            )
          : slotRegistry.replace(
              this.pluginId,
              item.name,
              item.render,
              item.opts,
            );
      item.deferred.bind(live);
    }
    this.close();
  }

  rollback(): void {
    if (this.closed) return;
    this.close();
  }

  private assertRegistration(pluginId: string, slot: string): void {
    if (pluginId !== this.pluginId) {
      throw new Error(
        `UI bundle for '${this.pluginId}' cannot register as '${pluginId}'`,
      );
    }
    if (!this.allowedSlots.has(slot)) {
      throw new Error(
        `Plugin '${pluginId}' did not declare UI slot '${slot}'`,
      );
    }
  }

  private assertOpen(): void {
    if (this.closed || active !== this) {
      throw new Error(`UI activation for '${this.pluginId}' is closed`);
    }
  }

  private close(): void {
    this.closed = true;
    if (active === this) active = undefined;
  }
}

export function registerUiSlot(
  pluginId: string,
  kind: SlotKind,
  name: SlotName,
  render: SlotRenderer,
  opts: SlotOpts = {},
): Disposable {
  if (active) return active.stage(pluginId, kind, name, render, opts);

  const allowed = declaredSlots.get(pluginId);
  if (allowed) {
    if (!allowed.has(name)) {
      throw new Error(`Plugin '${pluginId}' did not declare UI slot '${name}'`);
    }
    throw new Error(
      `Plugin '${pluginId}' must register UI slots during host activation`,
    );
  }
  return kind === "fill"
    ? slotRegistry.fill(pluginId, name, render, opts)
    : slotRegistry.replace(pluginId, name, render, opts);
}

export function clearUiContributionPolicy(pluginId: string): void {
  declaredSlots.delete(pluginId);
  if (active?.pluginId === pluginId) active.rollback();
}

export function resetUiContributionActivationForTests(): void {
  active?.rollback();
  active = undefined;
  declaredSlots.clear();
}
