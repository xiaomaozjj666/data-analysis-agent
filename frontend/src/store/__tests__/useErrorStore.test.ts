/// <reference types="vitest/globals" />
import { useErrorStore } from "../useErrorStore";

// Zustand stores are singletons; reset state between tests for isolation.
beforeEach(() => {
  useErrorStore.setState({ error: "", errorExpanded: false });
});

describe("useErrorStore", () => {
  it("has the correct initial state", () => {
    expect(useErrorStore.getState().error).toBe("");
    expect(useErrorStore.getState().errorExpanded).toBe(false);
  });

  it("setError('') sets error to an empty string", () => {
    useErrorStore.getState().setError("");
    expect(useErrorStore.getState().error).toBe("");
  });

  it("setError('msg') sets the error message", () => {
    useErrorStore.getState().setError("something broke");
    expect(useErrorStore.getState().error).toBe("something broke");
  });

  it("setError with a non-empty message resets errorExpanded to false", () => {
    useErrorStore.setState({ errorExpanded: true });
    useErrorStore.getState().setError("new error");
    expect(useErrorStore.getState().error).toBe("new error");
    expect(useErrorStore.getState().errorExpanded).toBe(false);
  });

  it("setError with an empty message does NOT reset errorExpanded", () => {
    useErrorStore.setState({ errorExpanded: true });
    useErrorStore.getState().setError("");
    expect(useErrorStore.getState().error).toBe("");
    expect(useErrorStore.getState().errorExpanded).toBe(true);
  });

  it("setErrorExpanded(true) sets errorExpanded", () => {
    useErrorStore.getState().setErrorExpanded(true);
    expect(useErrorStore.getState().errorExpanded).toBe(true);
  });

  it("setErrorExpanded(false) sets errorExpanded", () => {
    useErrorStore.setState({ errorExpanded: true });
    useErrorStore.getState().setErrorExpanded(false);
    expect(useErrorStore.getState().errorExpanded).toBe(false);
  });

  it("setErrorExpanded supports a functional updater", () => {
    useErrorStore.setState({ errorExpanded: false });
    useErrorStore.getState().setErrorExpanded((prev) => !prev);
    expect(useErrorStore.getState().errorExpanded).toBe(true);
    useErrorStore.getState().setErrorExpanded((prev) => !prev);
    expect(useErrorStore.getState().errorExpanded).toBe(false);
  });
});
