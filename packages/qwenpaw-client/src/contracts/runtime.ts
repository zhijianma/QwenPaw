import type { HostHandshake } from "../generated/hostHandshake.js";

export type RuntimeHandshake = HostHandshake;
export type RuntimeFeature = HostHandshake["features"][number];
