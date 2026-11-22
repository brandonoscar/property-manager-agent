/**
 * ⌘K command palette — typeahead for nav, actions, and theme.
 *
 * Uses ``cmdk`` (MIT) for the keyboard primitives. All commands route
 * through ``executeCommand`` so the palette stays purely presentational.
 */

import * as Dialog from '@radix-ui/react-dialog';
import { Command } from 'cmdk';
import type * as React from 'react';
import { useEffect, useMemo, useState } from 'react';
import { useCommandPaletteShortcut } from '../hooks/useCommandPaletteShortcut';
import {
  COMMANDS,
  type CommandGroup,
  devCommandsVisible,
  executeCommand,
} from '../lib/commands';
import { capturePaletteOpened } from '../services/analytics';

const GROUP_ORDER: CommandGroup[] = ['Navigate', 'Action'];

export function CommandPalette(): React.ReactElement {
  const [open, setOpen] = useState(false);

  // Bind ⌘K / Ctrl+K. The hook owns preventDefault.
  useCommandPaletteShortcut(() => {
    capturePaletteOpened('keyboard');
    setOpen((v) => !v);
  });

  // Close on Esc — cmdk's Dialog handles it but be defensive.
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent): void => {
      if (e.key === 'Escape') setOpen(false);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [open]);

  const grouped = useMemo(() => {
    const showDev = devCommandsVisible();
    const buckets = new Map<CommandGroup, typeof COMMANDS>();
    for (const cmd of COMMANDS) {
      if (cmd.devOnly && !showDev) continue; // hide "Dev:" commands in prod (#7 F5b)
      const list = buckets.get(cmd.group) ?? [];
      list.push(cmd);
      buckets.set(cmd.group, list);
    }
    return buckets;
  }, []);

  const handleSelect = async (id: string): Promise<void> => {
    setOpen(false);
    await executeCommand(id);
  };

  return (
    <Command.Dialog
      open={open}
      onOpenChange={setOpen}
      label="Command palette"
      className="cmdk-root"
    >
      {/* Radix Dialog requires a Title (and a Description) for screen readers;
          cmdk's Command.Dialog renders the Radix Dialog.Content but no title,
          so without these it logs a "DialogContent requires a DialogTitle"
          a11y warning (#7 F3). Visually hidden — no visual change. */}
      <Dialog.Title className="sr-only">Command palette</Dialog.Title>
      <Dialog.Description className="sr-only">
        Search and run navigation, actions, and theme commands.
      </Dialog.Description>
      <Command.Input
        placeholder="Type a command or search…"
        className="cmdk-input"
        autoFocus
      />
      <Command.List className="cmdk-list">
        <Command.Empty className="cmdk-empty">No commands match.</Command.Empty>

        {GROUP_ORDER.map((group) => {
          const items = grouped.get(group);
          if (!items || items.length === 0) return null;
          return (
            <Command.Group key={group} heading={group} className="cmdk-group">
              {items.map((cmd) => (
                <Command.Item
                  key={cmd.id}
                  value={`${cmd.label} ${(cmd.keywords ?? []).join(' ')}`}
                  onSelect={() => void handleSelect(cmd.id)}
                  className="cmdk-item"
                  aria-label={cmd.label}
                >
                  {cmd.label}
                </Command.Item>
              ))}
            </Command.Group>
          );
        })}
      </Command.List>
    </Command.Dialog>
  );
}
