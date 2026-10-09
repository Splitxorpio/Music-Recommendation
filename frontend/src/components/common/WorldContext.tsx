import { createContext, useContext } from "react";
import type { WorldOf } from "../../lib/themes";

/** Which world (visual theme) each sub-taste was given; set once when the profile loads. */
export const WorldContext = createContext<WorldOf>(() => null);
export const useWorldOf = () => useContext(WorldContext);
