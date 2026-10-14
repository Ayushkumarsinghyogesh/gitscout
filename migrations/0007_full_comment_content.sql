-- 0007_full_comment_content.sql
--
-- Comments now carry their own GitHub link and, for inline code-review comments, the
-- file they were left on. A fourth comment type is admitted: `review_comment`, the
-- inline comments on a pull request's review threads, which were never collected
-- before this migration (they live on a GraphQL connection the page query did not
-- read). Additive only; no existing row or column changes.
--
-- Rollback: 0007_full_comment_content.down.sql

alter table gitscout.comments add column if not exists url       text;
alter table gitscout.comments add column if not exists file_path text;

-- The check constraint was created unnamed, so it is found by its definition
-- rather than guessed by name.
do $$
declare
    cname text;
begin
    select conname into cname
      from pg_constraint
     where conrelid = 'gitscout.comments'::regclass
       and contype = 'c'
       and pg_get_constraintdef(oid) like '%comment_type%';
    if cname is not null then
        execute format('alter table gitscout.comments drop constraint %I', cname);
    end if;
    alter table gitscout.comments
        add constraint comments_comment_type_check
        check (comment_type in ('comment', 'review', 'reply', 'review_comment'));
end
$$;

comment on column gitscout.comments.url is
    'The comment''s own GitHub link, as returned by the API.';
comment on column gitscout.comments.file_path is
    'review_comment only: the file the review thread is on.';
comment on column gitscout.comments.comment_type is
    'comment = ordinary thread comment; review = a PR review verdict (body may be '
    'empty); reply = a discussion reply (parent_id = the comment); review_comment = '
    'an inline code comment on a PR (parent_id = the review).';
