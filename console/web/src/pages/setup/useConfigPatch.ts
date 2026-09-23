/**
 * One place the wizard writes configuration.
 *
 * Every step is "patch these paths, with this reason" — the wizard never assembles YAML and
 * never bypasses preview. A step that touches a protected path (git, live mode) surfaces
 * that as `needsStepUp`, and the caller shows the confirm dialog instead of saving.
 */

import { useCallback, useState } from 'react';

import { errorMessage } from '@/api';

import { configApi, type PatchOp, type PreviewResult } from '../settings/api';

export interface PatchOutcome {
  ok: boolean;
  message: string;
  preview?: PreviewResult;
}

export interface UseConfigPatch {
  busy: boolean;
  error: string | null;
  lastPreview: PreviewResult | null;
  /** Validate without writing — what a step's "Check" button calls. */
  check: (fileId: string, ops: PatchOp[]) => Promise<PreviewResult | null>;
  /** Preview, then save when the preview is valid and needs nothing more than a session. */
  apply: (
    fileId: string,
    ops: PatchOp[],
    reason: string,
    options?: { confirmPhrase?: string; applyEffects?: boolean },
  ) => Promise<PatchOutcome>;
}

export function useConfigPatch(): UseConfigPatch {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [lastPreview, setLastPreview] = useState<PreviewResult | null>(null);

  const check = useCallback(async (fileId: string, ops: PatchOp[]) => {
    setBusy(true);
    setError(null);
    try {
      const doc = await configApi.get(fileId);
      const preview = await configApi.preview(fileId, { patch: ops, base_sha: doc.sha });
      setLastPreview(preview);
      return preview;
    } catch (err) {
      setError(errorMessage(err));
      return null;
    } finally {
      setBusy(false);
    }
  }, []);

  const apply = useCallback(
    async (
      fileId: string,
      ops: PatchOp[],
      reason: string,
      options?: { confirmPhrase?: string; applyEffects?: boolean },
    ): Promise<PatchOutcome> => {
      setBusy(true);
      setError(null);
      try {
        const doc = await configApi.get(fileId);
        const preview = await configApi.preview(fileId, { patch: ops, base_sha: doc.sha });
        setLastPreview(preview);
        if (!preview.valid) {
          const message = preview.errors.map((e) => `${e.loc || 'document'}: ${e.msg}`).join('; ');
          setError(message);
          return { ok: false, message, preview };
        }
        if (preview.changed_paths.length === 0) {
          return { ok: true, message: 'Already set — nothing to save.', preview };
        }
        const result = await configApi.save(fileId, {
          base_sha: doc.sha,
          reason,
          patch: ops,
          apply_effects: options?.applyEffects ?? false,
          ...(options?.confirmPhrase ? { confirm_phrase: options.confirmPhrase } : {}),
        });
        return {
          ok: true,
          message: `Saved ${result.changed_paths.join(', ')}`,
          preview,
        };
      } catch (err) {
        const message = errorMessage(err);
        setError(message);
        return { ok: false, message };
      } finally {
        setBusy(false);
      }
    },
    [],
  );

  return { busy, error, lastPreview, check, apply };
}

export function replaceOps(entries: Array<[string, unknown]>): PatchOp[] {
  return entries.map(([dotted, value]) => ({
    op: 'replace',
    path: `/${dotted.split('.').join('/')}`,
    value,
  }));
}
