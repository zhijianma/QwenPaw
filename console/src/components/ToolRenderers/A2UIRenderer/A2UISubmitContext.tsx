import { createContext, useContext } from "react";

type SubmitFn = (query: string) => void;

export const A2UISubmitContext = createContext<SubmitFn | null>(null);

export function useA2UISubmit(): SubmitFn | null {
  return useContext(A2UISubmitContext);
}
