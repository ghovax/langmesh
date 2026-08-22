// Messages the session has not taken yet, and the one thing allowed to hand them over.

/** What became of one attempt to hand a message over. */
export type Delivery =
  /** The session took it. It is gone from the queue. */
  | "accepted"
  /** The session is parked on a decision and took nothing. It stays queued, and stays visible. */
  | "refused"
  /** A failed compaction blocks the session until the user retries it. */
  | "compaction"
  /** The attempt did not reach the session at all. It stays queued. */
  | "failed";

export interface OutboxMessage {
  id: string;
  text: string;
  dataParts: Record<string, unknown>[];
}

/** Why the queue is not moving, which is a different question from whether it is empty. */
export type OutboxHold =
  /** The session is parked on a decision. Answering it releases these. */
  | "decision"
  /** The last attempt never reached the session. It will go on the next attempt. */
  | "unreachable"
  /** A failed compaction is the only thing holding the session; retrying it is the only release. */
  | "compaction"
  /** Nothing is wrong: the queue is empty, or delivery is under way. */
  | null;

export interface OutboxState {
  /** In the order they were typed. Every one of these is undelivered — that is what being here means. */
  messages: OutboxMessage[];
  /** Taken by the session but not yet drawn back as its own row: still waiting, still visible. */
  handed: OutboxMessage[];
  hold: OutboxHold;
  /** The message being handed over right now: in the list, but going rather than waiting. */
  delivering: string | null;
}

export interface OutboxPorts {
  /** Hand one message to the session. Must not throw; a thrown error is treated as `failed`. */
  deliver: (message: OutboxMessage) => Promise<Delivery>;
  /** Whether the session is parked right now as the transcript sees it, an optimisation rather than the authority. */
  parked: () => boolean;
  /** Called whenever the queue or the blocked flag changes, so a view can re-render. */
  changed: (state: OutboxState) => void;
}

export class Outbox {
  private messages: OutboxMessage[] = [];
  // Handed over but not yet echoed by the session as a transcript row. It is taken, so it is no
  // longer in the delivery list, but it is not drawn until the session really records it.
  private handed = new Map<string, OutboxMessage>();
  private held: OutboxHold = null;
  private handing: string | null = null;
  // One attempt at a time, since two overlapping pumps would deliver the same head twice.
  private pumping = false;
  // The conversation these messages were typed into. Empty until one exists.
  private session = "";
  private settlements = new Map<string, (delivery: Delivery) => void>();

  constructor(private readonly ports: OutboxPorts) {}

  state(): OutboxState {
    return {
      messages: [...this.messages],
      handed: [...this.handed.values()],
      hold: this.held,
      delivering: this.handing,
    };
  }

  /** Point the queue at a conversation, keeping the messages when the session is only now coming into being. */
  retarget(session: string): void {
    if (session === this.session) return;
    const leavingRealConversation = this.session !== "";
    this.session = session;
    if (leavingRealConversation) this.clear();
  }

  /** A person typed something. */
  add(message: OutboxMessage): Promise<Delivery> {
    this.messages = [...this.messages, message];
    this.announce();
    // A held queue moves only through its matching explicit release. Adding another message cannot
    // implicitly retry a failed request, a refused decision, or a failed compaction. It was
    // accepted by the local queue immediately, so the composer must not claim an API send is active.
    if (this.held !== null) {
      return Promise.resolve(
        this.held === "decision" ? "refused" : this.held === "compaction" ? "compaction" : "failed",
      );
    }
    const settlement = new Promise<Delivery>((resolve) =>
      this.settlements.set(message.id, resolve),
    );
    void this.pump();
    return settlement;
  }

  /** The decision that was blocking delivery has been answered, which is one of the two retry triggers. */
  released(): void {
    if (this.held !== "decision") return;
    this.held = null;
    this.announce();
    void this.pump();
  }

  /** Try again after a failure to reach the session. The other person-caused trigger. */
  retry(): void {
    if (this.held !== "unreachable") return;
    this.held = null;
    this.announce();
    void this.pump();
  }

  /** A successful explicit compaction retry releases messages held by the failed compaction. */
  compactionRecovered(): void {
    if (this.held !== "compaction") return;
    this.held = null;
    this.announce();
    void this.pump();
  }

  /** A person removed a queued message before it went. */
  remove(id: string): void {
    const remaining = this.messages.filter((message) => message.id !== id);
    if (remaining.length === this.messages.length) return;
    this.messages = remaining;
    this.settle(id, "failed");
    this.announce();
  }

  /** The session recorded it as a transcript row, so the handed card retires. */
  echoed(id: string): void {
    if (!this.handed.delete(id)) return;
    this.announce();
  }

  /** Retire every hand-over the transcript now holds, given the transcript's current user row ids. */
  retireEchoed(userRowIds: ReadonlySet<string>): void {
    let retired = false;
    for (const id of Array.from(this.handed.keys())) {
      if (userRowIds.has(`user-${id}`)) {
        this.handed.delete(id);
        retired = true;
      }
    }
    if (retired) this.announce();
  }

  /** Switching to another session: this queue belonged to the one being left. */
  clear(): void {
    if (this.messages.length === 0 && this.handed.size === 0 && this.held === null) return;
    for (const message of this.messages) this.settle(message.id, "failed");
    this.messages = [];
    this.handed.clear();
    this.held = null;
    this.handing = null;
    this.announce();
  }

  private announce(): void {
    this.ports.changed(this.state());
  }

  private async pump(): Promise<void> {
    if (this.pumping || this.held !== null) return;
    this.pumping = true;
    try {
      while (this.messages.length > 0) {
        // Asked before each message, since answering one decision can leave another open.
        if (this.ports.parked()) {
          this.hold("decision");
          return;
        }
        const head = this.messages[0];
        // Marked before the await, so a view never draws it as waiting, and announced in the same tick.
        this.handing = head.id;
        this.announce();
        let outcome: Delivery;
        try {
          outcome = await this.ports.deliver(head);
        } catch {
          outcome = "failed";
        }
        this.handing = null;
        if (outcome === "refused") {
          this.settle(head.id, outcome);
          this.hold("decision");
          return;
        }
        if (outcome === "compaction") {
          this.settle(head.id, outcome);
          this.hold("compaction");
          return;
        }
        if (outcome === "failed") {
          // It never reached the session, so it keeps its place and says so.
          this.settle(head.id, outcome);
          this.hold("unreachable");
          return;
        }
        // Taken by the session: it leaves the delivery list but not the screen — it stays a
        // queued card until the session draws it back as its own row.
        this.handed.set(head.id, head);
        this.messages = this.messages.filter((message) => message.id !== head.id);
        this.settle(head.id, outcome);
        this.held = null;
        this.announce();
      }
    } finally {
      this.pumping = false;
    }
  }

  private hold(reason: OutboxHold): void {
    // Always announced, even when the reason is unchanged, because the state around it is not.
    this.held = reason;
    this.announce();
  }

  private settle(id: string, delivery: Delivery): void {
    this.settlements.get(id)?.(delivery);
    this.settlements.delete(id);
  }
}
