const lrc_pattern = /^\[\d+:\d+\.\d+]/;

function parse_lyrics(content) {
    const content_lines = content.split(/\r?\n/).filter(line => line.trim() !== '');
    return content_lines.map(line => {
        return {
            text: line,
            is_lrc: lrc_pattern.test(line)
        };
    });
}

function remove_lyric_time(lyric) {
    return lyric.replace(lrc_pattern, "").trim();
}

function format_lyric_time(current_time_f, total_time_f, offset) {
    current_time_f = Math.min(Math.max(current_time_f + offset, 0), total_time_f);
    const minutes = Math.floor(current_time_f / 60);
    const seconds = Math.floor(current_time_f % 60);
    const milliseconds = Math.floor((current_time_f % 1) * 100);
    
    // Add leading zeros if necessary
    const minutesStr = minutes < 10 ? '0' + minutes : minutes;
    const secondsStr = seconds < 10 ? '0' + seconds : seconds;
    const millisecondsStr = milliseconds < 10 ? '0' + milliseconds : milliseconds;
    
    return `[${minutesStr}:${secondsStr}.${millisecondsStr}]`;
}

function extension_name_lrc(filename) {
    const ext = ".lrc";
    const lastSlashIndex = Math.max(filename.lastIndexOf('/'), filename.lastIndexOf('\\'));
    const pathPart = lastSlashIndex >= 0 ? filename.substring(0, lastSlashIndex + 1) : '';
    const filePart = lastSlashIndex >= 0 ? filename.substring(lastSlashIndex + 1) : filename;
    return pathPart + (filePart.lastIndexOf('.') > 0 ? filePart.substring(0, filePart.lastIndexOf('.')) : filePart)  + ext;
}
