-- Rollback for 0007. Refuses if any review_comment rows exist, so data is never
-- silently dropped by a rollback.
do $$
begin
    if exists (select 1 from gitscout.comments where comment_type = 'review_comment') then
        raise exception 'gitscout.comments holds review_comment rows; delete them deliberately first';
    end if;
end
$$;
alter table gitscout.comments drop constraint if exists comments_comment_type_check;
alter table gitscout.comments
    add constraint comments_comment_type_check
    check (comment_type in ('comment', 'review', 'reply'));
alter table gitscout.comments drop column if exists file_path;
alter table gitscout.comments drop column if exists url;
