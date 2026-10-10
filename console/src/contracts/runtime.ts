import type { HostHandshake } from "./generated/hostHandshake";

export type RuntimeHandshake = HostHandshake;
export type RuntimeFeature = HostHandshake["features"][number];
