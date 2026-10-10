/** Transport-neutral Artifact and Evidence references shared by Console APIs. */
export interface ArtifactRef {
  artifact_id: string;
  kind: string;
  uri: string;
  media_type: string;
  content_hash: string;
  size_bytes: number;
  metadata: Record<string, unknown>;
}

export interface EvidenceRef {
  evidence_id: string;
  artifact_id: string;
  claim: string;
  producer: string;
  captured_at: string;
  metadata: Record<string, unknown>;
}

export interface ConversationArtifactLink {
  artifact_ref: ArtifactRef;
  evidence_ref: EvidenceRef;
  artifact_receipt: string;
}
