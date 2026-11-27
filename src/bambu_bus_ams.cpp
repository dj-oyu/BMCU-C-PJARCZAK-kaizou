#include "bambu_bus_ams.h"
#include <string.h>
#include "hal/irq_wch.h"
#include "ams.h"
#include "hal/time_hw.h"
#include "app_api.h"
#include "_bus_hardware.h"
#include "crc_bus.h"
#include "before_on_use_replay.h"
#include "bmcu_link.h"
#include "printer_motion_watch.h"
#include "bmcu_link_protocol.h"
#include "bmcu_ams_service_watch.h"
#include "printer_bus_result.h"
#include "Motion_control.h"
#include "ams_online_detect_policy.h"

uint8_t bambubus_ams_map[4] = {0, 1, 2, 3};
static void bambubus_build_static_serial(void);
// 64-bit ticks: a 32-bit deadline compared with a signed difference flips sign after
// 2^31 ticks (~119 s at 18 MHz), so a long silence would stop being reported as an
// error and motion control would resume the last motion the printer requested.
static uint64_t bambubus_heartbeat_deadline = 0u;

void bambubus_heartbeat_seen_fast(void)
{
    bambubus_heartbeat_deadline = time_ticks64() + ms_to_ticks32(1000u);
}

bool package_check_crc16(uint8_t *data, int data_length)
{
    if (data_length < 4) return false;

    const int crc_off = data_length - 2;
    const uint16_t num = bus_crc16(data, (uint32_t)crc_off);

    return (data[crc_off] == (num & 0xFFu)) &&
           (data[crc_off + 1] == ((num >> 8) & 0xFFu));
}

void bambubus_init()
{
    bambubus_build_static_serial();
    bambubus_heartbeat_deadline = 0u;
}

void package_add_crc(uint8_t *data, int send_data_length) // 为数据包添加crc校验
{
    if (data[1] & 0x80) // 获取数据包头位置
    {

        data[3] = bus_crc8(data, 3); // 短帧头校验
    }
    else
    {
        data[6] = bus_crc8(data, 6); // 长帧头校验
    }
    send_data_length -= 2;                                              // 校验位之前的数据长度
    uint16_t num = bus_crc16(data, (uint32_t)send_data_length); // 计算crc16校验
    data[(send_data_length)] = num & 0xFF;                              // crc16校验低字节
    data[(send_data_length + 1)] = num >> 8;                            // crc16校验高字节
}

struct bambubus_long_packge_data
{
    uint16_t package_number;
    uint16_t package_length;
    uint8_t crc8;
    uint16_t target_address;
    uint16_t source_address;
    uint16_t type;
    uint8_t *datas;
    uint16_t data_length;
} __attribute__((packed));

void bambubus_long_package_get(bambubus_long_packge_data *data)
{
    if (bus_port_to_host.send_data_len != 0) return;
    uint8_t* out = bus_port_to_host.tx_build_buf();

    out[0] = 0x3D;
    out[1] = 0x00;

    data->package_length = data->data_length + 15;
    memcpy(out + 2,  data,        11);
    memcpy(out + 13, data->datas, data->data_length);

    package_add_crc(out, data->data_length + 15);
    bus_port_to_host.send_data_len = data->data_length + 15;
}

void bambubus_long_package_analysis(uint8_t *buf, int data_length, bambubus_long_packge_data *data)
{
    if (data_length < 15) {
        memset(data, 0, sizeof(*data));
        return;
    }
    memcpy(data, buf + 2, 11);
    data->datas = buf + 13;
    data->data_length = data_length - 15; // +2byte CRC16
}

bambubus_long_packge_data printer_data_long;

static uint8_t bounded_payload_hash(const uint8_t* data, uint16_t length)
{
    // FNV-1a over a bounded prefix: stable evidence without copying raw printer
    // payloads or adding unbounded work to the parser path.
    uint8_t hash = 0xC5u;
    const uint16_t bounded = length < 32u ? length : 32u;
    for (uint16_t i = 0u; i < bounded; ++i)
        hash = static_cast<uint8_t>((hash ^ data[i]) * 0x93u);
    return hash;
}
// A printer that still holds our registration keeps polling this AMS well
// inside the 1000 ms heartbeat window; one that forgot it goes silent towards
// us while still broadcasting registration queries (the HMS 0500_409D state).
static constexpr uint32_t kOnlineDetectConfirmSettleMs = 3000u;
static constexpr uint32_t kOnlineDetectServiceSilenceMs = 1500u;

static uint8_t online_detect_prefix_now = 0x0Cu;
static ams_online_detect::State online_detect_state;

static inline void online_detect_reset(void)
{
    ams_online_detect::reset(online_detect_state);
    online_detect_prefix_now = 0x0Cu;
    bmcu_link_ams_registration_reset();
}

bambubus_package_type get_packge_type(unsigned char *buf, int length)
{
    if (length < 6) return bambubus_package_type::none;
    if (buf[0] != 0x3D) return bambubus_package_type::none;
    if (!package_check_crc16(buf, length)) return bambubus_package_type::none;

    if (buf[1] == 0xC5)
    {
        switch (buf[4])
        {
        case 0x03:
            return bambubus_package_type::filament_motion_short;
        case 0x04:
            return bambubus_package_type::filament_motion_long;
        case 0x05:
            return bambubus_package_type::online_detect;
        case 0x06:
            return bambubus_package_type::REQx6;
        case 0x07:
            return bambubus_package_type::NFC_detect;
        case 0x08:
            return bambubus_package_type::set_filament_info;
        case 0x20:
            return bambubus_package_type::heartbeat;
        default:
            return bambubus_package_type::ETC;
        }
    }
    else if ((buf[1] == 0x05) || (buf[1] == 0x04))
    {
        if (length < 15) return bambubus_package_type::none;
        bambubus_long_package_analysis(buf, length, &printer_data_long);
        if (printer_data_long.target_address != host_device_type_ams)
        {
            return bambubus_package_type::none;
        }

        switch (printer_data_long.type)
        {
        case 0x21A:
            return bambubus_package_type::MC_online;
        case 0x211:
            return bambubus_package_type::read_filament_info;
        case 0x218:
            return bambubus_package_type::set_filament_info_type2;
        case 0x103:
            return bambubus_package_type::version;
        case 0x402:
            return bambubus_package_type::serial_number;
        default:
            return bambubus_package_type::ETC;
        }
    }
    return bambubus_package_type::none;
}
uint8_t package_num = 0;

uint8_t get_filament_left_char(const _ams *ams)
{
    uint8_t data = 0u;

    for (uint8_t i = 0; i < 4u; i++)
    {
        if (!ams->filament[i].online) continue;

        data |= (uint8_t)(1u << (i << 1));
        if (ams->filament[i].motion != _filament_motion::idle)
            data |= (uint8_t)(2u << (i << 1));
    }

    return data;
}

static uint32_t time_sendout_onuse_ticks[4] = {};
static uint8_t last_before_on_use_motion_flag = 0x00;
static uint8_t count_on_use = 0u;

static inline uint8_t bmcu_pressure_class(uint16_t pressure)
{
    if (pressure == 0xF06Fu) return 2u;
    if (pressure == 0xFFFFu || pressure == 0xFF74u) return 0u;
    return 1u;
}

class BmcuStatusNotifyGuard
{
public:
    ~BmcuStatusNotifyGuard()
    {
        bmcu_link_status_changed(BMCU_STATUS_CHANGE_SLOT |
                                 BMCU_STATUS_CHANGE_MOTION |
                                 BMCU_STATUS_CHANGE_PRESSURE);
    }
};
bool set_motion(unsigned char read_num, unsigned char statu_flags, unsigned char fliment_motion_flag, uint8_t ams_num)
{
    const uint8_t fixed_ams_num = (uint8_t)BAMBU_BUS_AMS_NUM;
    if (ams_num != fixed_ams_num) return false;

    _ams *ams_ptr = &ams[bambubus_ams_map[fixed_ams_num]];
    BmcuStatusNotifyGuard status_guard;

    if (read_num < 4)
    {
        const uint8_t ch = (uint8_t)read_num;

        const bool is_send_out      = ((statu_flags == 0x03) && (fliment_motion_flag == 0x00));
        const bool is_before_on_use = ((statu_flags == 0x09) && ((fliment_motion_flag == 0x7F) || (fliment_motion_flag == 0xA5)));
        const bool is_stop_on_use   = ((statu_flags == 0x07) && (fliment_motion_flag == 0x00));
        const bool is_on_use        = ((statu_flags == 0x07) && (fliment_motion_flag == 0x7F));
        const bool is_before_pullb  = ((statu_flags == 0x09) && (fliment_motion_flag == 0x3F));

        uint32_t &t_sendout_onuse = time_sendout_onuse_ticks[ch];

        const uint8_t loaded = ams_state_get_loaded();
        const bool allow_any = (loaded == 0xFFu) || (loaded == ch);
        const bool allow_stop = (loaded == ch);

        const bool accept =
            (is_send_out) ||
            (is_before_on_use && allow_any) ||
            (is_on_use        && allow_any) ||
            (is_stop_on_use   && allow_stop) ||
            (is_before_pullb  && allow_stop);

        if (accept)
        {
            if (ams_ptr->now_filament_num != ch)
            {
                if (ams_ptr->now_filament_num < 4)
                {
                    const uint8_t prev = ams_ptr->now_filament_num;
                    ams_ptr->filament[prev].motion = _filament_motion::idle;
                    ams_ptr->filament_use_flag = 0x00;
                    ams_ptr->pressure = 0xF9C6;
                    time_sendout_onuse_ticks[prev] = 0u;
                }
                bus_now_ams_num = bambubus_ams_map[fixed_ams_num];
                ams_ptr->now_filament_num = ch;
            }
        }

        if (is_send_out)
        {
            t_sendout_onuse = 0u;
            count_on_use = 0u;

            const _filament_motion prev = ams_ptr->filament[ch].motion;

            // Not set_unloaded(0xFF): this is ch claiming the merger, not the
            // session ending, and the two need different answers when the
            // current owner is in TAIL. See ams_state_preempt in app_api.h.
            if (prev != _filament_motion::send_out && ams_state_get_loaded() != 0xFFu)
                ams_state_preempt(ch);

            ams_ptr->filament[ch].motion = _filament_motion::send_out;
            ams_ptr->filament_use_flag = 0x02;
            ams_ptr->pressure = 0x4700;
        }
        else if (is_before_on_use)
        {
            t_sendout_onuse = 0u;
            count_on_use = 0u;

            if (!allow_any) return true;

            last_before_on_use_motion_flag = fliment_motion_flag;

            const _filament_motion prev = ams_ptr->filament[ch].motion;

            ams_ptr->filament[ch].motion = _filament_motion::before_on_use;
            ams_ptr->filament_use_flag = 0x04;

            if (fliment_motion_flag == 0x7F)
            {
                ams_ptr->pressure = 0x1E34;
            }
            else
            {
                ams_ptr->pressure = (prev == _filament_motion::send_out) ? 0x4700 : 0x2B00;
            }

            ams_state_set_loaded(ch);
        }
        else if (is_stop_on_use)
        {
            t_sendout_onuse = 0u;

            if (!allow_stop) return true;

            const _filament_motion prev = ams_ptr->filament[ch].motion;

            if (prev == _filament_motion::on_use ||
                prev == _filament_motion::before_on_use)
            {
                ams_ptr->filament[ch].motion = _filament_motion::stop_on_use;
            }

            ams_ptr->filament_use_flag = 0x04;
            ams_ptr->pressure = 0x2B00;

            if (prev == _filament_motion::before_on_use && last_before_on_use_motion_flag == 0x7F)
            {
                ams_ptr->pressure = 0x1E34;
            }
        }
        else if (is_on_use)
        {
            if (!allow_any) return true;

            const _filament_motion prev = ams_ptr->filament[ch].motion;

            if (prev == _filament_motion::send_out)
            {
                if (time_hw_tpms != 0u)
                {
                    const uint32_t now = time_ticks32();
                    if (t_sendout_onuse == 0u) t_sendout_onuse = now;

                    const uint32_t dt = (uint32_t)(now - t_sendout_onuse);
                    const uint32_t lim = 15000u * (uint32_t)time_hw_tpms;

                    if (dt < lim)
                    {
                        ams_ptr->filament_use_flag = 0x04;
                        ams_ptr->pressure = 0x2B00;
                        return true;
                    }

                    t_sendout_onuse = 0u;
                }
                else
                {
                    ams_ptr->filament_use_flag = 0x04;
                    ams_ptr->pressure = 0x2B00;
                    return true;
                }
            }

            t_sendout_onuse = 0u;

            ams_ptr->filament[ch].motion = _filament_motion::on_use;
            ams_ptr->filament_use_flag = 0x04;

            if (ams_ptr->pressure != 0xF06Fu) ams_ptr->pressure = 0x2B00;

            if (last_before_on_use_motion_flag == 0x7F && count_on_use < 5)
            {
                count_on_use++;
                ams_ptr->pressure = 0x1E34;
            }

            ams_state_set_loaded(ch);
        }
        else if (is_before_pullb)
        {
            t_sendout_onuse = 0u;

            if (!allow_stop) return true;

            const _filament_motion prev = ams_ptr->filament[ch].motion;

            if (prev == _filament_motion::on_use ||
                prev == _filament_motion::before_on_use ||
                prev == _filament_motion::stop_on_use)
            {
                ams_ptr->filament[ch].motion = _filament_motion::before_pull_back;
            }

            ams_ptr->filament_use_flag = 0x04;
            ams_ptr->pressure = 0x2B00;

            ams_state_set_unloaded(ch);
        }
        else if (statu_flags == 0x09)
        {
            t_sendout_onuse = 0u;
            ams_ptr->filament_use_flag = 0x04;
            ams_ptr->pressure = 0x2B00;
        }
    }
    else if (read_num == 0xFF)
    {
        if ((statu_flags == 0x03) && (fliment_motion_flag == 0x00))
        {
            if (ams_ptr->now_filament_num < 4)
            {
                const uint8_t ch = ams_ptr->now_filament_num;
                const _filament_motion m = ams_ptr->filament[ch].motion;

                time_sendout_onuse_ticks[ch] = 0u;

                if (m == _filament_motion::before_pull_back ||
                    m == _filament_motion::on_use ||
                    m == _filament_motion::before_on_use ||
                    m == _filament_motion::stop_on_use)
                {
                    ams_ptr->filament[ch].motion = _filament_motion::pull_back;
                    ams_ptr->filament_use_flag = 0x02;
                }

                ams_ptr->pressure = 0x4700;
                ams_state_set_unloaded(ch);
            }
        }
        else if (statu_flags == 0x01)
        {
            const uint8_t ch = ams_ptr->now_filament_num;
            // Session idle, not a commanded retract: nothing is being pulled,
            // so a tail still in the merger stays there. See ams_state_session_idle.
            if (ch < 4 && ams_ptr->filament_use_flag != 0x04)
                ams_state_session_idle(ch);
        }
        else
        {
            if (ams_ptr->now_filament_num < 4)
            {
                const uint8_t ch = ams_ptr->now_filament_num;
                const _filament_motion m = ams_ptr->filament[ch].motion;

                if (m == _filament_motion::on_use ||
                    m == _filament_motion::before_on_use ||
                    m == _filament_motion::stop_on_use)
                {
                    return true;
                }
            }

            for (uint8_t i = 0; i < 4; i++)
            {
                ams_ptr->filament[i].motion = _filament_motion::idle;
                time_sendout_onuse_ticks[i] = 0u;
            }

            ams_ptr->filament_use_flag = 0x00;
            ams_ptr->pressure = 0xF9C6;
            ams_ptr->now_filament_num = 0xFF;
            // The same session-idle intent as :435, spelled without a channel
            // because this path has just forgotten which one it was using.
            ams_state_session_idle(0xFFu);
        }
    }

    return true;
}
// 3D C5 0C C8 03 00 07 00 7F 02 36 54
struct bambubus_printer_motion_package_struct
{
    uint8_t magic_byte;
    uint8_t flag;
    uint8_t length;
    uint8_t crc8;
    uint8_t command;
    uint8_t ams_num;
    uint8_t statu_flag;
    uint8_t filamnet_channel;
    uint8_t motion_flag;
    uint8_t unknow;
    uint16_t crc16;
} __attribute__((packed));
struct bambubus_ams_motion_package_struct
{
    uint8_t magic_byte = 0x3D;
    uint8_t flag = 0xC0;
    uint8_t length = 0x2C;
    uint8_t crc8;
    uint8_t command = 0x03;
    uint8_t ams_num = 0;
    uint8_t unknow1 = 0x00;
    uint8_t filament_use_flag = 0x00;
    uint8_t filament_channel = 0x00;
    float meters = 0;
    uint16_t pressure = 0;
    uint16_t unknow2 = 0xFFFF;
    uint8_t unknow3[12];
    uint8_t filament_stu_flag = 0x00;
    uint32_t last1 = 0xFFFFFFFF;
    uint32_t last2 = 0x01010101;
    uint8_t filament_channel_2 = 0x00;
    uint8_t last4 = 0x00;
    uint16_t last5 = 0x0000;
    uint16_t crc16;
} __attribute__((packed));
// 3D F0 2C C1 03 00 00 00 FF 00 00 00 00 6F F0 FB FF 36 00 00 00 F8 FF F7 FF 00 00 27 00 55 F8 EE F9 F0 B7 BA B9 B2 00 00 00 00 88 E6
// 3D D0 2C D1 03 03 00 02 00 00 00 80 3F FF FF FF FF 36 00 00 00 00 00 00 00 00 00 27 00 55 FF FF FF FF 01 01 01 01 00 00 00 00 15 95
static const bambubus_ams_motion_package_struct _bambubus_ams_motion_package_struct_init_data = {
    0x3D,       // magic_byte
    0xC0,       // flag
    0x2C,       // length
    0x00,       // crc8
    0x03,       // command
    0x00,       // ams_num
    0x00,       // unknow1
    0x00,       // filament_use_flag
    0x00,       // filament_channel
    0.0f,       // meters
    0x0000,     // pressure
    0xFFFF,     // unknow2
    {           // unknow3[12]
        0x36, 0x00, 0x00, 0x00, 0xF8, 0xFF, 0xF7, 0xFF, 0x00, 0x00, 0x27, 0x00
    },
    0x00,           // filament_stu_flag
    0xF6F2F4FB,    // last1
    0xB1B4B7B5,    // last2
    0x00,           // filament_channel_2
    0x00,           // last4
    0x0000,         // last5
    0x0000          // crc16
};


static uint8_t before_on_use_sniff_7f_active = 0u;
static uint8_t before_on_use_sniff_7f_index  = 0u;
static uint8_t before_on_use_sniff_7f_channel = 0xFFu;

void get_package_motion(bambubus_printer_motion_package_struct *package_recv)
{
    // Observe-only: counted on address match alone, before the TX-busy guard and
    // before the online/set_motion filters, so our own state can never fabricate
    // apparent printer silence. See docs/HMS_0500_409D_CAPTURE_RUNBOOK.md.
    if (package_recv->ams_num == (uint8_t)BAMBU_BUS_AMS_NUM)
        bmcu_link_ams_service_frame(bmcu_ams_service::kServiceMotion);
    bmcu_link_printer_motion_cmd(printer_motion_watch::kKindMotion, package_recv->ams_num, package_recv->filamnet_channel,
                                 package_recv->statu_flag, package_recv->motion_flag);

    if (bus_port_to_host.send_data_len != 0) return;
    uint8_t *out = bus_port_to_host.tx_build_buf();

    bambubus_printer_motion_package_struct in;
    memcpy(&in, package_recv, sizeof(in));

    const uint8_t fixed_ams_num = (uint8_t)BAMBU_BUS_AMS_NUM;
    if (in.ams_num != fixed_ams_num) return;
    ams_online_detect::note_service_traffic(online_detect_state, time_ticks32());

    const uint8_t ams_idx = bambubus_ams_map[fixed_ams_num];
    if (!ams[ams_idx].online) return;

    _ams *ams_ptr = &ams[ams_idx];
    if (!set_motion(in.filamnet_channel, in.statu_flag, in.motion_flag, fixed_ams_num)) return;

    auto *package_send = (bambubus_ams_motion_package_struct *)out;
    memcpy(package_send, &_bambubus_ams_motion_package_struct_init_data, sizeof(*package_send));

    const uint8_t ch = ams_ptr->now_filament_num;
    const bool is_idle = (ch == 0xFF);
    const uint16_t pressure = is_idle ? 0xFF74 : ams_ptr->pressure;

    package_send->flag = 0xC0 | (uint8_t)(package_num << 3);
    package_send->ams_num = fixed_ams_num;
    package_send->filament_use_flag = is_idle ? 0x00 : ams_ptr->filament_use_flag;
    package_send->filament_channel = ch;
    package_send->filament_channel_2 = ch;

    if (ch < 4u)
    {
        const float meters = Motion_control_get_filament_meters(ch);
        memcpy(&package_send->meters, &meters, sizeof(package_send->meters));
    }

    memcpy(&package_send->pressure, &pressure, sizeof(pressure));

    if (is_idle)
        package_send->unknow2 = 0x0008;

    package_send->filament_stu_flag = get_filament_left_char(ams_ptr);

    const bool replay_before_on_use_7f =
        (ch < 4u) &&
        (ams_ptr->filament[ch].motion == _filament_motion::before_on_use) &&
        (ams_ptr->filament_use_flag == 0x04u) &&
        (last_before_on_use_motion_flag == 0x7Fu);

    if (!replay_before_on_use_7f)
    {
        before_on_use_sniff_7f_active = 0u;
        before_on_use_sniff_7f_index = 0u;
        before_on_use_sniff_7f_channel = 0xFFu;
    }

    if (replay_before_on_use_7f)
    {
        if (!before_on_use_sniff_7f_active || before_on_use_sniff_7f_channel != ch)
        {
            before_on_use_sniff_7f_active = 1u;
            before_on_use_sniff_7f_index = 0u;
            before_on_use_sniff_7f_channel = ch;
        }

        const uint8_t rows_count = before_on_use_replay::kRowCount;
        const uint8_t idx = before_on_use_sniff_7f_index;

        if (idx < rows_count)
        {
            before_on_use_replay::Row row;
            before_on_use_replay::unpack(idx, &row);

            package_send->pressure = row.pressure;
            package_send->unknow2 = row.unknow2;
            memcpy(package_send->unknow3, row.unknow3, sizeof(row.unknow3));

            // The sniffed rows are bytes 29..36 of the real AMS's reply. Byte
            // 29 is filament_stu_flag, and the sniffed unit's value (0x57: ch0
            // in use, four spools online) is not ours, so it is not replayed.
            uint8_t tail[8];
            memcpy(tail, &row.last1, sizeof(row.last1));
            memcpy(tail + 4, &row.last2, sizeof(row.last2));
            memcpy(out + 30, tail + 1, 7u);

            before_on_use_sniff_7f_index = (uint8_t)(idx + 1u);
        }
        else
        {
            package_send->pressure = 0x1E34u;
            package_send->unknow2 = 0x0AFDu;
        }
    }
    else if (pressure == 0xF06Fu)
    {
        package_send->pressure = 0xF06Fu;
        package_send->unknow2 = 0x1CE7u;
    }

    package_num = (package_num < 7u) ? (uint8_t)(package_num + 1u) : 0u;

    package_add_crc(out, sizeof(bambubus_ams_motion_package_struct));
    bus_port_to_host.send_data_len = sizeof(bambubus_ams_motion_package_struct);
}

// 3D C5 0D F1 04 00 01 00 03 FF 00 B2 C4
struct bambubus_printer_stu_motion_package_struct
{
    uint8_t magic_byte;
    uint8_t flag;
    uint8_t length;
    uint8_t crc8;
    uint8_t command;
    uint8_t ams_num;
    uint8_t statu_flag;
    uint8_t motion_flag;
    uint8_t unknow1;
    uint8_t filamnet_channel;
    uint8_t unknow2;
    uint16_t crc16;
} __attribute__((packed));
static_assert(sizeof(bambubus_printer_stu_motion_package_struct) == 13, "packed size mismatch");

struct bambubus_ams_stu_motion_package_struct
{
    uint8_t magic_byte = 0x3D;
    uint8_t flag = 0xC0;
    uint8_t length = 0x3C;
    uint8_t crc8;
    uint8_t command = 0x04;
    uint8_t ams_num_stu = 0;
    uint16_t temperature = 0; // 温度，单位0.1℃
    uint8_t humidity = 0;     // 湿度，单位%
    uint8_t filament_online_flag[3];
    uint8_t filament_channel_stu = 0x00;
    uint8_t filament_flag_wait_NFC = 0x00;
    uint8_t unknow_stu[3];
    uint8_t ams_num = 0;
    uint8_t unknow1 = 0x00;
    uint8_t filament_use_flag = 0x00;
    uint8_t filament_channel = 0x00;
    float meters = 0;
    uint16_t pressure = 0;
    uint16_t unknow2 = 0xFFFF;
    uint8_t unknow3[12];
    uint8_t filament_stu_flag = 0x00;
    uint32_t last1 = 0xFFFFFFFF;
    uint32_t last2 = 0x01010101;
    uint32_t last3 = 0x00000000;
    uint32_t last4 = 0xFFFFFFFF;
    uint16_t crc16;
} __attribute__((packed));

static const bambubus_ams_stu_motion_package_struct _bambubus_ams_stu_motion_package_struct_init_data = {
    0x3D,       // magic_byte
    0xC0,       // flag
    0x3C,       // length
    0x00,       // crc8
    0x04,       // command
    0x00,       // ams_num_stu
    0x0000,     // temperature
    0x00,       // humidity
    {0x00,0x00,0x00}, // filament_online_flag[3]
    0x00,       // filament_channel_stu
    0x00,       // filament_flag_wait_NFC
    {0x00,0x00,0x00}, // unknow_stu[3]
    0x00,       // ams_num
    0x00,       // unknow1
    0x00,       // filament_use_flag
    0x00,       // filament_channel
    0.0f,       // meters
    0x0000,     // pressure
    0xFFFF,     // unknow2
    {           // unknow3[12]
        0x36, 0x00, 0x00, 0x00, 0xF9, 0xFF, 0xF8, 0xFF, 0x00, 0x00, 0x27, 0x00
    },
    0x00,           // filament_stu_flag
    0xF6F2F4FB,    // last1
    0xB1B4B7B5,    // last2
    0x00000000,    // last3
    0xFFFFFFFF,    // last4
    0x0000          // crc16
};
void get_package_stu_motion(bambubus_printer_stu_motion_package_struct *package_recv)
{
    // Observe-only; same placement rationale as get_package_motion.
    if (package_recv->ams_num == (uint8_t)BAMBU_BUS_AMS_NUM)
        bmcu_link_ams_service_frame(bmcu_ams_service::kServiceStuMotion);
    bmcu_link_printer_motion_cmd(printer_motion_watch::kKindStuMotion, package_recv->ams_num, package_recv->filamnet_channel,
                                 package_recv->statu_flag, package_recv->motion_flag);

    if (bus_port_to_host.send_data_len != 0) return;
    uint8_t *out = bus_port_to_host.tx_build_buf();

    bambubus_printer_stu_motion_package_struct in;
    memcpy(&in, package_recv, sizeof(in));

    unsigned char filament_flag_on  = 0x00;
    unsigned char filament_flag_NFC = 0x00;

    const uint8_t fixed_ams_num = (uint8_t)BAMBU_BUS_AMS_NUM;
    if (in.ams_num != fixed_ams_num) return;
    ams_online_detect::note_service_traffic(online_detect_state, time_ticks32());

    const uint8_t ams_idx = bambubus_ams_map[fixed_ams_num];
    if (!ams[ams_idx].online) return;

    _ams *ams_ptr = &ams[ams_idx];

    for (uint8_t i = 0; i < 4u; i++)
        if (ams_ptr->filament[i].online)
            filament_flag_on |= (uint8_t)(1u << i);

    if (!set_motion(in.filamnet_channel, in.statu_flag, in.motion_flag, fixed_ams_num)) return;

    auto *package_send = (bambubus_ams_stu_motion_package_struct *)out;
    memcpy(package_send, &_bambubus_ams_stu_motion_package_struct_init_data, sizeof(*package_send));

    int16_t temperature = (int16_t)(
        ams_ptr->filament[0].compartment_temperature +
        ams_ptr->filament[1].compartment_temperature +
        ams_ptr->filament[2].compartment_temperature +
        ams_ptr->filament[3].compartment_temperature
    );
    temperature = (int16_t)(temperature * 10);
    if (temperature < 0) temperature = 0;
    temperature = (int16_t)(temperature >> 2);

    uint16_t humidity = (uint16_t)(
        ams_ptr->filament[0].compartment_humidity +
        ams_ptr->filament[1].compartment_humidity +
        ams_ptr->filament[2].compartment_humidity +
        ams_ptr->filament[3].compartment_humidity
    );
    humidity = (uint16_t)(humidity >> 2);

    const uint8_t ch = ams_ptr->now_filament_num;
    const bool is_idle = (ch == 0xFF);
    const uint16_t pressure = is_idle ? 0xFF74 : ams_ptr->pressure;

    package_send->flag = 0xC0 | (uint8_t)(package_num << 3);
    package_send->ams_num_stu = fixed_ams_num;
    package_send->temperature = (uint16_t)temperature;
    package_send->humidity = (uint8_t)humidity;

    const uint8_t on = (uint8_t)(filament_flag_on - filament_flag_NFC);
    package_send->filament_online_flag[0] = on;
    package_send->filament_online_flag[1] = on;
    package_send->filament_online_flag[2] = on;

    package_send->filament_channel_stu = in.filamnet_channel;
    package_send->filament_flag_wait_NFC = filament_flag_NFC;

    package_send->ams_num = fixed_ams_num;
    package_send->filament_use_flag = is_idle ? 0x00 : ams_ptr->filament_use_flag;
    package_send->filament_channel = ch;

    if (ch < 4)
    {
        const float meters = Motion_control_get_filament_meters(ch);
        memcpy(&package_send->meters, &meters, sizeof(package_send->meters));
    }

    memcpy(&package_send->pressure, &pressure, sizeof(pressure));

    package_send->filament_stu_flag = get_filament_left_char(ams_ptr);

    if (pressure == 0xF06Fu)
    {
        package_send->pressure = 0xF06Fu;
        package_send->unknow2 = 0x1CE7u;
    }

    package_add_crc(out, sizeof(bambubus_ams_stu_motion_package_struct));

    package_num = (package_num < 7u) ? (uint8_t)(package_num + 1u) : 0u;

    bus_port_to_host.send_data_len = sizeof(bambubus_ams_stu_motion_package_struct);
}

uint8_t online_detect_res[29] = {
    0x3D, 0xC0, 0x1D, 0xB4, 0x05, 0x01, 0x00,
    0x0D, 0x0E, 0xA0, '5', '5', '0', '0', 0x00, 0x00, '0', '0', '0', '0', 0xFF, 0xFF, 0xFF, 0xFF, 0x00, 0x00, 0x00,
    0x33, 0xF0
};

extern unsigned char long_packge_version_serial_number[];

static inline void online_detect_build_packet(const uint8_t ams_num, const uint8_t subtype)
{
    online_detect_res[0] = 0x3D;
    online_detect_res[1] = 0xC0;
    online_detect_res[2] = 29;
    online_detect_res[3] = 0xB4;
    online_detect_res[4] = 0x05;
    online_detect_res[5] = subtype;
    online_detect_res[6] = ams_num;
    online_detect_res[7] = online_detect_prefix_now;
    memcpy(online_detect_res + 8, long_packge_version_serial_number + 33, 16);
    package_add_crc(online_detect_res, 29);
}

bool get_package_online_detect(unsigned char *buf, int length)
{
    (void)length;
    // Observe-only: every registration query the printer emits is counted,
    // including those the registration policy below answers with silence.
    if (buf[5] == 0x00u) bmcu_link_ams_registration_query();

    if (bus_port_to_host.send_data_len != 0) return true;

    const uint8_t ams_num = (uint8_t)BAMBU_BUS_AMS_NUM;
    if (ams_num >= 4u) return true;

    if (ams[bambubus_ams_map[ams_num]].online != true)
    {
        online_detect_reset();
        return true;
    }

    if (buf[5] == 0x00)
    {
        const ams_online_detect::QueryAction act =
            ams_online_detect::on_query(online_detect_state);
        if (act == ams_online_detect::QueryAction::stay_silent) return true;

        online_detect_prefix_now =
            (act == ams_online_detect::QueryAction::offer_first) ? 0x0Cu : 0x0Au;

        online_detect_build_packet(ams_num, 0x00);

        uint8_t *out = bus_port_to_host.tx_build_buf();
        memcpy(out, online_detect_res, 29);
        bus_port_to_host.send_data_len = 29;
        return false;
    }

    if (buf[5] != 0x01) return true;
    if (buf[6] != ams_num) return true;

    online_detect_prefix_now = 0x0Au;
    online_detect_build_packet(ams_num, 0x01);

    if (memcmp(online_detect_res + 7, buf + 7, 17) != 0)
        return true;

    ams_online_detect::latch_confirm(online_detect_state, time_ticks32());
    bmcu_link_ams_registration_confirm(); // observe-only

    uint8_t *out = bus_port_to_host.tx_build_buf();
    memcpy(out, online_detect_res, 29);
    bus_port_to_host.send_data_len = 29;
    return false;
}

void get_package_long_packge_MC_online(unsigned char *buf, int length)
{
    (void)buf;
    (void)length;

    const uint8_t fixed_ams_num = (uint8_t)BAMBU_BUS_AMS_NUM;

    // Observe-only; same placement rationale as get_package_motion.
    if (printer_data_long.data_length >= 1u && printer_data_long.datas[0] == fixed_ams_num)
        bmcu_link_ams_service_frame(bmcu_ams_service::kServiceMcOnline);

    if (printer_data_long.data_length < 1u) return;
    if (!ams[bambubus_ams_map[fixed_ams_num]].online) return;
    if (printer_data_long.datas[0] != fixed_ams_num) return;
    ams_online_detect::note_service_traffic(online_detect_state, time_ticks32());

    unsigned char resp[6] = {fixed_ams_num, 0x00, 0x00, 0x00, 0x00, 0x00};

    bambubus_long_packge_data data;
    data.datas = resp;
    data.data_length = sizeof(resp);
    data.package_number = printer_data_long.package_number;
    data.type = printer_data_long.type;
    data.source_address = printer_data_long.target_address;
    data.target_address = printer_data_long.source_address;

    bambubus_long_package_get(&data);
}
unsigned char long_packge_filament[] =
    {
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x47, 0x46, 0x42, 0x30, 0x30, 0x00, 0x00, 0x00,
        0x41, 0x42, 0x53, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0xDD, 0xB1, 0xD4, 0xFF, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x18, 0x01, 0xF0, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00};
void get_package_long_packge_filament(unsigned char *buf, int length)
{
    (void)buf;
    (void)length;

    bambubus_long_packge_data data;

    const uint8_t fixed_ams_num = (uint8_t)BAMBU_BUS_AMS_NUM;
    const uint8_t ams_num = printer_data_long.datas[0];
    const uint8_t filament_num = printer_data_long.datas[1];

    if (ams_num != fixed_ams_num || filament_num >= 4 || ams[bambubus_ams_map[fixed_ams_num]].online != true)
    {
        return;
    }

    _ams *ams_ptr = ams + bambubus_ams_map[fixed_ams_num];
    long_packge_filament[0] = fixed_ams_num;
    long_packge_filament[1] = filament_num;
    memcpy(long_packge_filament + 19, ams_ptr->filament[filament_num].bambubus_filament_id, sizeof(ams_ptr->filament[filament_num].bambubus_filament_id));
    memcpy(long_packge_filament + 27, ams_ptr->filament[filament_num].name, sizeof(ams_ptr->filament[filament_num].name));
    long_packge_filament[59] = ams_ptr->filament[filament_num].color_R;
    long_packge_filament[60] = ams_ptr->filament[filament_num].color_G;
    long_packge_filament[61] = ams_ptr->filament[filament_num].color_B;
    long_packge_filament[62] = ams_ptr->filament[filament_num].color_A;
    memcpy(long_packge_filament + 79, &ams_ptr->filament[filament_num].temperature_max, 2);
    memcpy(long_packge_filament + 81, &ams_ptr->filament[filament_num].temperature_min, 2);

    data.datas = long_packge_filament;
    data.data_length = sizeof(long_packge_filament);
    data.package_number = printer_data_long.package_number;
    data.type = printer_data_long.type;
    data.source_address = printer_data_long.target_address;
    data.target_address = printer_data_long.source_address;

    bambubus_long_package_get(&data);
}

unsigned char long_packge_version_serial_number[] = {15,
                                                     '0', 'E', 'A', '0', '3', '0', '3', '0', '3', '0', '3', '0', '0', '0', '0',
                                                     0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
                                                     0x00,
                                                     0x0E, 0xA0, 0x30, 0x30, 0x30, 0x30, 0x00, 0x00,
                                                     0x30, 0x30, 0x30, 0x30,
                                                     0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF,
                                                     0x00,
                                                     0xFF, 0x00, 0xFF, 0x00, 0xFF, 0x00,
                                                     0x00};

static void bambubus_build_static_serial(void)
{
    static const char hex[] = "0123456789ABCDEF";
    volatile const uint8_t *uid = (volatile const uint8_t *)0x1FFFF7E8;
    const uint8_t ams_num = (uint8_t)BAMBU_BUS_AMS_NUM;

    uint64_t v = 1469598103934665603ull;
    for (int i = 0; i < 12; i++)
    {
        v ^= uid[i];
        v *= 1099511628211ull;
    }

    v ^= v >> 30;
    v *= 0xBF58476D1CE4E5B9ull;
    v ^= v >> 27;
    v *= 0x94D049BB133111EBull;
    v ^= v >> 31;

    long_packge_version_serial_number[0] = 15;
    long_packge_version_serial_number[1] = '0';
    long_packge_version_serial_number[2] = 'E';
    long_packge_version_serial_number[3] = 'A';
    long_packge_version_serial_number[4] = '0' + ams_num;

    {
        const uint8_t b0 = (uint8_t)(v >> 56);
        const uint8_t b1 = (uint8_t)(v >> 48);
        const uint8_t b2 = (uint8_t)(v >> 40);
        const uint8_t b3 = (uint8_t)(v >> 32);
        const uint8_t b4 = (uint8_t)(v >> 24);
        const uint8_t b5 = (uint8_t)(v >> 16);

        long_packge_version_serial_number[5]  = hex[(b0 >> 4) & 0x0F];
        long_packge_version_serial_number[6]  = hex[b0 & 0x0F];
        long_packge_version_serial_number[7]  = hex[(b1 >> 4) & 0x0F];
        long_packge_version_serial_number[8]  = hex[b1 & 0x0F];
        long_packge_version_serial_number[9]  = hex[(b2 >> 4) & 0x0F];
        long_packge_version_serial_number[10] = hex[b2 & 0x0F];
        long_packge_version_serial_number[11] = hex[(b3 >> 4) & 0x0F];
        long_packge_version_serial_number[12] = hex[b3 & 0x0F];
        long_packge_version_serial_number[13] = hex[(b4 >> 4) & 0x0F];
        long_packge_version_serial_number[14] = hex[b4 & 0x0F];
        long_packge_version_serial_number[15] = hex[(b5 >> 4) & 0x0F];
    }

    long_packge_version_serial_number[33] = 0x0E;
    long_packge_version_serial_number[34] = (uint8_t)(0xA0 + ams_num);
    long_packge_version_serial_number[35] = (uint8_t)(v >> 56);
    long_packge_version_serial_number[36] = (uint8_t)(v >> 48);
    long_packge_version_serial_number[37] = (uint8_t)(v >> 40);
    long_packge_version_serial_number[38] = (uint8_t)(v >> 32);
    long_packge_version_serial_number[39] = (uint8_t)(v >> 24);
    long_packge_version_serial_number[40] = (uint8_t)(v >> 16);
    long_packge_version_serial_number[41] = (uint8_t)(v >> 24);
    long_packge_version_serial_number[42] = (uint8_t)(v >> 16);
    long_packge_version_serial_number[43] = (uint8_t)(v >> 8);
    long_packge_version_serial_number[44] = (uint8_t)(v >> 0);
    long_packge_version_serial_number[45] = 0xFF;
    long_packge_version_serial_number[46] = 0xFF;
    long_packge_version_serial_number[47] = 0xFF;
    long_packge_version_serial_number[48] = 0xFF;
    long_packge_version_serial_number[65] = ams_num;

    online_detect_reset();
    online_detect_res[6] = ams_num;
    memcpy(online_detect_res + 8, long_packge_version_serial_number + 33, 16);
}

void get_package_long_packge_serial_number(unsigned char *buf, int length)
{
    (void)buf;
    (void)length;

    const uint8_t ams_num = (uint8_t)BAMBU_BUS_AMS_NUM;

    if ((printer_data_long.data_length > 33) && (printer_data_long.datas[33] != ams_num))
    {
        return;
    }

    if (ams[bambubus_ams_map[ams_num]].online != true)
    {
        return;
    }

    bambubus_long_packge_data data;
    data.datas = long_packge_version_serial_number;
    data.data_length = sizeof(long_packge_version_serial_number);
    data.package_number = printer_data_long.package_number;
    data.type = printer_data_long.type;
    data.source_address = printer_data_long.target_address;
    data.target_address = printer_data_long.source_address;
    bambubus_long_package_get(&data);
}

//0x0A // 10
//0x14 // 20
//0x1E // 30
//0x28 // 40
//0x32 // 50
//0x3C // 60
//0x46 // 70
//0x50 // 80
//0x5A // 90
unsigned char long_packge_version_version_and_name_AMS08[] = {0x00, 0x00, 0x32, 0x0A , // verison number
                                                              0x41, 0x4D, 0x53, 0x30, 0x38, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00};
//unsigned char long_packge_version_version_and_name_AMS2PRO[] = {
//    0x00, 0x00, 0x00, 0x5A,
//    0x4E, 0x33, 0x46, 0x30, 0x35, 0x00, 0x00, 0x00,
//    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00
//};


void get_package_long_packge_version(unsigned char *buf, int length)
{
    (void)buf;
    (void)length;

    const uint8_t fixed_ams_num = (uint8_t)BAMBU_BUS_AMS_NUM;
    const uint8_t ams_num = printer_data_long.datas[0];

    if (ams_num != fixed_ams_num || ams[bambubus_ams_map[fixed_ams_num]].online != true)
        return;

    long_packge_version_version_and_name_AMS08[sizeof(long_packge_version_version_and_name_AMS08) - 1u] = fixed_ams_num;

    bambubus_long_packge_data data;
    data.datas = long_packge_version_version_and_name_AMS08;
    data.data_length = (uint16_t)sizeof(long_packge_version_version_and_name_AMS08);
    data.package_number = printer_data_long.package_number;
    data.type = printer_data_long.type;
    data.source_address = printer_data_long.target_address;
    data.target_address = printer_data_long.source_address;

    bambubus_long_package_get(&data);
}

unsigned char set_filament_res[] = {0x3D, 0xC0, 0x08, 0xB2, 0x08, 0x60, 0xB4, 0x04};
void get_package_set_filament(unsigned char *buf, int length)
{
    (void)length;

    if (bus_port_to_host.send_data_len != 0) return;
    uint8_t* out = bus_port_to_host.tx_build_buf();
    uint8_t b = buf[5];

    const uint8_t fixed_ams_num = (uint8_t)BAMBU_BUS_AMS_NUM;
    uint8_t ams_num  = (b >> 4) & 0x0F;
    uint8_t read_num = (b >> 0) & 0x0F;

    if (ams_num != fixed_ams_num || read_num >= 4 || ams[bambubus_ams_map[fixed_ams_num]].online != true) return;

    _ams *ams_ptr = ams + bambubus_ams_map[fixed_ams_num];
    memcpy(ams_ptr->filament[read_num].bambubus_filament_id, buf + 7, sizeof(ams_ptr->filament[read_num].bambubus_filament_id));
    ams_ptr->filament[read_num].color_R = buf[15];
    ams_ptr->filament[read_num].color_G = buf[16];
    ams_ptr->filament[read_num].color_B = buf[17];
    ams_ptr->filament[read_num].color_A = buf[18];
    memcpy(&ams_ptr->filament[read_num].temperature_min, buf + 19, 2);
    memcpy(&ams_ptr->filament[read_num].temperature_max, buf + 21, 2);
    memcpy(ams_ptr->filament[read_num].name, buf + 23, sizeof(ams_ptr->filament[read_num].name));
    ams_ptr->filament[read_num].name[19] = 0;
    memcpy(out, set_filament_res, sizeof(set_filament_res));
    bus_port_to_host.send_data_len = sizeof(set_filament_res);
}
unsigned char set_filament_res_type2[] = {0x00, 0x00, 0x00};
void get_package_set_filament_type2(unsigned char *buf, int length)
{
    if (bus_port_to_host.send_data_len != 0) return;
    (void)buf;
    (void)length;

    bambubus_long_packge_data data;

    const uint8_t fixed_ams_num = (uint8_t)BAMBU_BUS_AMS_NUM;
    const uint8_t ams_num  = printer_data_long.datas[0];
    const uint8_t read_num = printer_data_long.datas[1];

    if (ams_num != fixed_ams_num || read_num >= 4 || ams[bambubus_ams_map[fixed_ams_num]].online != true) return;

    _ams *ams_ptr = ams + bambubus_ams_map[fixed_ams_num];

    memcpy(ams_ptr->filament[read_num].bambubus_filament_id,
           printer_data_long.datas + 2,
           sizeof(ams_ptr->filament[read_num].bambubus_filament_id));

    ams_ptr->filament[read_num].color_R = printer_data_long.datas[10];
    ams_ptr->filament[read_num].color_G = printer_data_long.datas[11];
    ams_ptr->filament[read_num].color_B = printer_data_long.datas[12];
    ams_ptr->filament[read_num].color_A = printer_data_long.datas[13];

    memcpy(&ams_ptr->filament[read_num].temperature_min, printer_data_long.datas + 14, 2);
    memcpy(&ams_ptr->filament[read_num].temperature_max, printer_data_long.datas + 16, 2);
    memset(ams_ptr->filament[read_num].name, 0, sizeof(ams_ptr->filament[read_num].name));
    memcpy(ams_ptr->filament[read_num].name, printer_data_long.datas + 18, 16);
    ams_ptr->filament[read_num].name[19] = 0;

    set_filament_res_type2[0] = fixed_ams_num;
    set_filament_res_type2[1] = read_num;
    set_filament_res_type2[2] = 0x00;

    data.datas = set_filament_res_type2;
    data.data_length = sizeof(set_filament_res_type2);

    data.package_number = printer_data_long.package_number;
    data.type = printer_data_long.type;
    data.source_address = printer_data_long.target_address;
    data.target_address = printer_data_long.source_address;

    bambubus_long_package_get(&data);
}

bambubus_package_type bambubus_run()
{
    bambubus_package_type stu = bambubus_package_type::none;

    static uint64_t last_hb_deadline = 0u;
    const uint64_t now = time_ticks64();
    // Observe-only: keeps the poll cadence far below the ~238 s SysTick wrap even
    // during total printer silence, which is exactly the case being measured.
    bmcu_link_ams_service_poll();

    {
        const ams_online_detect::Config cfg = {
            ms_to_ticks32(kOnlineDetectConfirmSettleMs),
            ms_to_ticks32(kOnlineDetectServiceSilenceMs),
        };
        ams_online_detect::poll(online_detect_state, cfg, (uint32_t)now);  // policy keeps 32-bit wrap-safe ticks
    }

    int rx_len = 0;
    _bus_data_type t = _bus_data_type::none;
    uint8_t *buf = nullptr;

    {
        const uint32_t s = irq_save_wch();
        rx_len = bus_port_to_host.recv_data_len;
        t = bus_port_to_host.bus_package_type;
        buf = bus_port_to_host.bus_recv_data_ptr;
        irq_restore_wch(s);
    }

    if (rx_len > 0 && t == _bus_data_type::bambubus)
    {
        if (buf != nullptr && rx_len <= 1280 && buf[0] == 0x3D)
        {
            const int len = rx_len;

            stu = get_packge_type(buf, len);
            const bool response_pending_before = bus_port_to_host.send_data_len != 0;
            bool handler_dispatched = false;
            bool no_response_expected = false;

            // Which AMS the frame is addressed to. Every handler checks this
            // after dispatch and returns silently on a mismatch, so a poll
            // meant for the other BMCU on the bus is indistinguishable from a
            // genuine failure to answer -- both land as FAILED/NO_RESPONSE.
            // On a two-unit bus that is most of the traffic: 26,000 such
            // events in thirty minutes of ordinary printing, every one of them
            // queued as a durable record for delivery. Capturing the address
            // here lets the reporting path tell the two apart.
            uint8_t addressed_ams = 0xFFu;
            if (stu == bambubus_package_type::filament_motion_short)
                addressed_ams = ((const bambubus_printer_motion_package_struct *)buf)->ams_num;
            else if (stu == bambubus_package_type::filament_motion_long)
                addressed_ams = ((const bambubus_printer_stu_motion_package_struct *)buf)->ams_num;
            else if (len > 15 && (buf[1] == 0x05u || buf[1] == 0x04u))
                addressed_ams = buf[15];

            switch (stu)
            {
            case bambubus_package_type::filament_motion_short:
                handler_dispatched = true;
                get_package_motion((bambubus_printer_motion_package_struct *)buf);
                break;

            case bambubus_package_type::filament_motion_long:
                handler_dispatched = true;
                get_package_stu_motion((bambubus_printer_stu_motion_package_struct *)buf);
                break;

            case bambubus_package_type::online_detect:
                handler_dispatched = true;
                no_response_expected = get_package_online_detect(buf, len);
                break;

            case bambubus_package_type::MC_online:
                handler_dispatched = true;
                get_package_long_packge_MC_online(buf, len);
                break;

            case bambubus_package_type::read_filament_info:
                handler_dispatched = true;
                get_package_long_packge_filament(buf, len);
                break;

            case bambubus_package_type::version:
                handler_dispatched = true;
                get_package_long_packge_version(buf, len);
                break;

            case bambubus_package_type::serial_number:
                handler_dispatched = true;
                get_package_long_packge_serial_number(buf, len);
                break;

            case bambubus_package_type::set_filament_info:
                handler_dispatched = true;
            {
                const uint8_t b = buf[5];
                const uint8_t ams_num = (b >> 4) & 0x0F;
                const uint8_t fil = (b >> 0) & 0x0F;

                get_package_set_filament(buf, len);

                if (ams_num == (uint8_t)BAMBU_BUS_AMS_NUM && fil < 4)
                    ams_datas_set_need_to_save_filament(fil);
                break;
            }

            case bambubus_package_type::set_filament_info_type2:
                handler_dispatched = true;
                get_package_set_filament_type2(buf, len);
                if (printer_data_long.datas[0] == (uint8_t)BAMBU_BUS_AMS_NUM && printer_data_long.datas[1] < 4)
                    ams_datas_set_need_to_save_filament(printer_data_long.datas[1]);
                break;

            default:
                break;
            }

            const uint16_t response_length = bus_port_to_host.send_data_len > 0
                ? static_cast<uint16_t>(bus_port_to_host.send_data_len) : 0u;
            uint8_t command = len > 4 ? buf[4] : 0u;
            if (len > 12 && (buf[1] == 0x04u || buf[1] == 0x05u)) command = buf[11];

            printer_bus_result::HandlerDisposition disposition =
                printer_bus_result::HandlerDisposition::unsupported;
            if (stu == bambubus_package_type::none)
                disposition = printer_bus_result::HandlerDisposition::no_handler;
            else if (handler_dispatched)
                disposition = no_response_expected
                    ? printer_bus_result::HandlerDisposition::no_response_expected
                    : printer_bus_result::HandlerDisposition::response_expected;

            const printer_bus_result::TransactionResult result =
                printer_bus_result::classify_transaction(
                    disposition, response_pending_before, response_length);
            const bmcu_link_protocol::TransactionOutcome outcome = result.outcome;
            const bmcu_link_protocol::DecisionReason reason = result.reason;
            if (reason == bmcu_link_protocol::REASON_TX_BUSY)
                bus_port_to_host.report_tx_fault(bus_tx_fault::response_busy);
            else if (reason == bmcu_link_protocol::REASON_NO_RESPONSE)
                bus_port_to_host.report_tx_fault(bus_tx_fault::response_missing);
            else if (reason == bmcu_link_protocol::REASON_NO_RESPONSE_EXPECTED)
                bus_port_to_host.report_tx_fault(bus_tx_fault::no_response_expected);
            bmcu_link_printer_transaction(
                static_cast<uint8_t>(stu), command, static_cast<uint8_t>(outcome),
                static_cast<uint8_t>(reason), static_cast<uint16_t>(len), response_length,
                addressed_ams);

            if (len >= 15 && (buf[1] == 0x04u || buf[1] == 0x05u))
            {
                bmcu_link_printer_long_transaction(
                    printer_data_long.type, static_cast<uint8_t>(outcome),
                    static_cast<uint8_t>(reason), printer_data_long.datas,
                    printer_data_long.data_length, response_length,
                    bounded_payload_hash(printer_data_long.datas, printer_data_long.data_length));
            }

            if (response_length != 0u) bus_port_to_host.defer_send_us(50u);
        }

        {
            const uint32_t s = irq_save_wch();
            bus_port_to_host.release_recv_frame();
            irq_restore_wch(s);
        }
    }

    uint64_t hb_deadline = 0u;
    {
        const uint32_t s = irq_save_wch();
        hb_deadline = bambubus_heartbeat_deadline;
        irq_restore_wch(s);
    }

    if (stu == bambubus_package_type::none && hb_deadline != last_hb_deadline)
    {
        last_hb_deadline = hb_deadline;
        if (hb_deadline > now)
            stu = bambubus_package_type::heartbeat;
    }

    if (now > hb_deadline)
    {
        stu = bambubus_package_type::error;
        // Heartbeat lost: when the bus returns the printer re-runs AMS
        // registration, and a still-latched registration would swallow that
        // query unanswered (recurring HMS 0500_409D at print start that only a
        // power cycle cleared). Re-arm immediately while the bus is quiet; the
        // stale-registration re-offer in ams_online_detect is the belt-and-
        // braces path for the case where the heartbeat never lapses.
        online_detect_reset();
    }

    return stu;
}
