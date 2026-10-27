/* 资源搜索与资源链接共用的 qBittorrent 添加。WebAPI 留在服务端。 */
import { apiSend } from '../../api';

export const QBITTORRENT_ADD_URL = '/api/configuration/downloads/qbittorrent/add';
export const QBITTORRENT_SENT = '已发送到 qBittorrent';

export interface QbittorrentAddResult {
  ok: boolean;
  info_hash: string;
  pending: boolean;
}

export function submitQbittorrentMagnet(magnet: string, signal?: AbortSignal) {
  return apiSend<QbittorrentAddResult>(QBITTORRENT_ADD_URL, { magnet }, 'POST', signal);
}
