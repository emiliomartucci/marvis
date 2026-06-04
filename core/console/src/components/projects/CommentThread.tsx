"use client";

import { useState, useEffect, useCallback } from "react";
import SafeMarkdown from "./SafeMarkdown";
import {
  getComments,
  createComment,
  updateComment,
  deleteComment,
  addReaction,
  removeReaction,
} from "@/lib/api";
import type {
  CommentResponse,
  CommentCreate,
  CommentStatus,
  TargetType,
  ReactionType,
  CommentReaction,
} from "@/lib/types";

const STATUS_COLORS: Record<CommentStatus, string> = {
  info: "text-pir-text-muted",
  question: "text-pir-warning",
  blocker: "text-pir-error",
  resolved: "text-pir-success",
};

const REACTION_LABELS: Record<ReactionType, string> = {
  "+1": "\u{1F44D}",
  "-1": "\u{1F44E}",
  eyes: "\u{1F440}",
  check: "\u2705",
};

function ReactionBar({
  reactions,
  currentUser,
  onToggle,
}: {
  reactions: CommentReaction[];
  currentUser: string;
  onToggle: (reaction: ReactionType) => void;
}) {
  const grouped: Record<string, { count: number; byMe: boolean }> = {};
  for (const r of reactions) {
    if (!grouped[r.reaction]) grouped[r.reaction] = { count: 0, byMe: false };
    grouped[r.reaction].count++;
    if (r.created_by === currentUser) grouped[r.reaction].byMe = true;
  }

  return (
    <div className="flex items-center gap-1 mt-1">
      {(Object.keys(REACTION_LABELS) as ReactionType[]).map((key) => {
        const data = grouped[key];
        return (
          <button
            key={key}
            onClick={() => onToggle(key)}
            className={`text-caption px-1.5 py-0.5 rounded border transition-colors ${
              data?.byMe
                ? "border-pir-accent bg-pir-accent/10 text-pir-accent"
                : "border-pir text-pir-text-muted hover:border-pir-accent/50"
            }`}
          >
            {REACTION_LABELS[key]}
            {data && data.count > 0 ? ` ${data.count}` : ""}
          </button>
        );
      })}
    </div>
  );
}

function CommentItem({
  comment,
  currentUser,
  onReply,
  onUpdate,
  onDelete,
  onReaction,
  depth,
}: {
  comment: CommentResponse;
  currentUser: string;
  onReply: (parentId: number) => void;
  onUpdate: (id: number, body: string, status: CommentStatus) => void;
  onDelete: (id: number) => void;
  onReaction: (commentId: number, reaction: ReactionType, add: boolean) => void;
  depth: number;
}) {
  const [editing, setEditing] = useState(false);
  const [editBody, setEditBody] = useState(comment.body);
  const [editStatus, setEditStatus] = useState<CommentStatus>(comment.status);
  const isOwner = comment.created_by === currentUser;
  const isDeleted = comment.body === "[deleted]";

  function handleSaveEdit() {
    onUpdate(comment.id, editBody, editStatus);
    setEditing(false);
  }

  function handleToggleReaction(reaction: ReactionType) {
    const myReaction = comment.reactions.find(
      (r) => r.reaction === reaction && r.created_by === currentUser
    );
    onReaction(comment.id, reaction, !myReaction);
  }

  return (
    <div className={`${depth > 0 ? "ml-6 border-l border-pir pl-3" : ""}`}>
      <div className="bg-pir-surface-1 border border-pir rounded p-3 mb-1">
        {/* Header */}
        <div className="flex items-center gap-2 mb-1">
          <span className="text-caption font-medium text-pir-text-secondary">{comment.created_by}</span>
          <span className={`text-caption ${STATUS_COLORS[comment.status]}`}>{comment.status}</span>
          <span className="text-caption text-pir-text-muted ml-auto">
            {new Date(comment.created_at).toLocaleDateString()}
          </span>
          {comment.edited_at && (
            <span className="text-caption text-pir-text-muted">(edited)</span>
          )}
        </div>

        {/* Body */}
        {editing ? (
          <div className="space-y-2">
            <textarea
              value={editBody}
              onChange={(e) => setEditBody(e.target.value)}
              className="w-full bg-pir-surface-0 border border-pir rounded px-2 py-1 text-caption text-pir-text-primary min-h-[60px] resize-y"
            />
            <div className="flex items-center gap-2">
              <select
                value={editStatus}
                onChange={(e) => setEditStatus(e.target.value as CommentStatus)}
                className="text-caption bg-pir-surface-0 border border-pir rounded px-1 py-0.5 text-pir-text-primary"
              >
                <option value="info">info</option>
                <option value="question">question</option>
                <option value="blocker">blocker</option>
                <option value="resolved">resolved</option>
              </select>
              <button
                onClick={handleSaveEdit}
                className="text-caption px-2 py-0.5 bg-pir-accent text-white rounded"
              >
                Save
              </button>
              <button
                onClick={() => setEditing(false)}
                className="text-caption px-2 py-0.5 text-pir-text-muted"
              >
                Cancel
              </button>
            </div>
          </div>
        ) : (
          <div className="text-caption text-pir-text-secondary">
            {isDeleted ? (
              <span className="italic text-pir-text-muted">[deleted]</span>
            ) : (
              <SafeMarkdown content={comment.body} />
            )}
          </div>
        )}

        {/* Actions */}
        {!isDeleted && !editing && (
          <div className="flex items-center gap-3 mt-2">
            <ReactionBar
              reactions={comment.reactions}
              currentUser={currentUser}
              onToggle={handleToggleReaction}
            />
            <div className="flex items-center gap-2 ml-auto">
              {depth === 0 && (
                <button
                  onClick={() => onReply(comment.id)}
                  className="text-caption text-pir-text-muted hover:text-pir-accent"
                >
                  Reply
                </button>
              )}
              {isOwner && (
                <>
                  <button
                    onClick={() => {
                      setEditBody(comment.body);
                      setEditStatus(comment.status);
                      setEditing(true);
                    }}
                    className="text-caption text-pir-text-muted hover:text-pir-accent"
                  >
                    Edit
                  </button>
                  <button
                    onClick={() => onDelete(comment.id)}
                    className="text-caption text-pir-text-muted hover:text-pir-error"
                  >
                    Delete
                  </button>
                </>
              )}
            </div>
          </div>
        )}
      </div>

      {/* Replies */}
      {comment.replies?.map((reply) => (
        <CommentItem
          key={reply.id}
          comment={reply}
          currentUser={currentUser}
          onReply={onReply}
          onUpdate={onUpdate}
          onDelete={onDelete}
          onReaction={onReaction}
          depth={depth + 1}
        />
      ))}
    </div>
  );
}

export default function CommentThread({
  targetType,
  targetId,
  currentUser,
}: {
  targetType: TargetType;
  targetId: string;
  currentUser: string;
}) {
  const [comments, setComments] = useState<CommentResponse[]>([]);
  const [loading, setLoading] = useState(true);
  const [newBody, setNewBody] = useState("");
  const [newStatus, setNewStatus] = useState<CommentStatus>("info");
  const [replyTo, setReplyTo] = useState<number | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const fetchComments = useCallback(() => {
    const controller = new AbortController();
    getComments(targetType, targetId, { signal: controller.signal })
      .then(setComments)
      .catch(() => {})
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return controller;
  }, [targetType, targetId]);

  useEffect(() => {
    const controller = fetchComments();
    return () => controller.abort();
  }, [fetchComments]);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!newBody.trim() || submitting) return;
    setSubmitting(true);
    try {
      const data: CommentCreate = {
        target_type: targetType,
        target_id: targetId,
        body: newBody.trim(),
        status: newStatus,
        parent_id: replyTo,
      };
      await createComment(data);
      setNewBody("");
      setReplyTo(null);
      setNewStatus("info");
      fetchComments();
    } catch {
      // ignore
    } finally {
      setSubmitting(false);
    }
  }

  async function handleUpdate(id: number, body: string, status: CommentStatus) {
    try {
      await updateComment(id, { body, status });
      fetchComments();
    } catch {
      // ignore
    }
  }

  async function handleDelete(id: number) {
    try {
      await deleteComment(id);
      fetchComments();
    } catch {
      // ignore
    }
  }

  async function handleReaction(commentId: number, reaction: ReactionType, add: boolean) {
    try {
      if (add) {
        await addReaction(commentId, reaction);
      } else {
        await removeReaction(commentId, reaction);
      }
      fetchComments();
    } catch {
      // ignore
    }
  }

  if (loading) return <div className="text-pir-text-muted text-caption p-2">Loading comments...</div>;

  return (
    <div className="space-y-3">
      {/* Comment list */}
      {comments.length === 0 && (
        <div className="text-pir-text-muted text-caption">No comments yet.</div>
      )}
      {comments.map((c) => (
        <CommentItem
          key={c.id}
          comment={c}
          currentUser={currentUser}
          onReply={(parentId) => setReplyTo(parentId)}
          onUpdate={handleUpdate}
          onDelete={handleDelete}
          onReaction={handleReaction}
          depth={0}
        />
      ))}

      {/* New comment form */}
      <form onSubmit={handleSubmit} className="bg-pir-surface-0 border border-pir rounded p-3">
        {replyTo && (
          <div className="flex items-center gap-2 mb-2">
            <span className="text-caption text-pir-text-muted">Replying to #{replyTo}</span>
            <button
              type="button"
              onClick={() => setReplyTo(null)}
              className="text-caption text-pir-error"
            >
              Cancel
            </button>
          </div>
        )}
        <textarea
          value={newBody}
          onChange={(e) => setNewBody(e.target.value)}
          placeholder="Write a comment... (markdown supported)"
          className="w-full bg-pir-surface-1 border border-pir rounded px-3 py-2 text-caption text-pir-text-primary placeholder:text-pir-text-muted min-h-[60px] resize-y"
        />
        <div className="flex items-center gap-2 mt-2">
          <select
            value={newStatus}
            onChange={(e) => setNewStatus(e.target.value as CommentStatus)}
            className="text-caption bg-pir-surface-1 border border-pir rounded px-2 py-1 text-pir-text-primary"
          >
            <option value="info">info</option>
            <option value="question">question</option>
            <option value="blocker">blocker</option>
            <option value="resolved">resolved</option>
          </select>
          <button
            type="submit"
            disabled={submitting || !newBody.trim()}
            className="ml-auto px-3 py-1 text-caption bg-pir-accent text-white rounded hover:bg-pir-accent/80 disabled:opacity-50"
          >
            {submitting ? "Posting..." : replyTo ? "Reply" : "Comment"}
          </button>
        </div>
      </form>
    </div>
  );
}
