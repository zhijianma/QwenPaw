import React from "react";
import type { CommandSuggestion } from "../types";
import styles from "./MessageInput.module.less";

interface CommandSuggestionsProps {
  suggestions: CommandSuggestion[];
  onSelect: (cmd: CommandSuggestion) => void;
  onDismiss: () => void;
}

const CommandSuggestions: React.FC<CommandSuggestionsProps> = ({
  suggestions,
  onSelect,
  onDismiss,
}) => {
  return (
    <div className={styles.suggestionsPanel}>
      {suggestions.map((cmd) => (
        <div
          key={cmd.command}
          className={styles.suggestionItem}
          onClick={() => onSelect(cmd)}
          role="option"
          tabIndex={0}
          onKeyDown={(e) => {
            if (e.key === "Enter") onSelect(cmd);
            if (e.key === "Escape") onDismiss();
          }}
        >
          <span className={styles.suggestionCommand}>{cmd.command}</span>
          <span className={styles.suggestionDescription}>
            {cmd.description}
          </span>
        </div>
      ))}
    </div>
  );
};

export default CommandSuggestions;
