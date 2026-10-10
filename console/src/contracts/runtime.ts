export type RuntimeFeature =
  | "artifact.references"
  | "capability.catalog"
  | "chat.interactions"
  | "task.event-cursor"
  | "task.runtime";

export interface RuntimeHandshake {
  schema: "qwenpaw.host-handshake.v1";
  product: "qwenpaw";
  version: string;
  protocol_version: 1;
  features: string[];
}
