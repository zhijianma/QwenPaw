import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("../config", () => ({
  getApiUrl: vi.fn((path: string) => `http://test${path}`),
}));
vi.mock("../authHeaders", () => ({
  buildAuthHeaders: vi.fn(() => ({})),
}));

import { pawappApi } from "./pawapp";

function response(status: number, body: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as Response;
}

describe("pawapp API", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("confirms the exact release before uninstalling", async () => {
    global.fetch = vi
      .fn()
      .mockResolvedValueOnce(
        response(428, {
          detail: {
            code: "capability_deactivation_authorization_required",
            release_hash: "sha256:pawapp-1",
          },
        }),
      )
      .mockResolvedValueOnce(response(200, {}));

    await expect(pawappApi.uninstall("app-1")).resolves.toBeUndefined();

    expect(fetch).toHaveBeenCalledTimes(2);
    expect(fetch).toHaveBeenNthCalledWith(2, "http://test/pawapps/app-1", {
      method: "DELETE",
      headers: {
        "X-QwenPaw-Confirm-Release": "sha256:pawapp-1",
      },
    });
  });
});
