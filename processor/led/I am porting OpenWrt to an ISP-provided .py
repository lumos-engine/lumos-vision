I am porting OpenWrt to an ISP-provided XPON/GPON Wi-Fi router/ONT that I own.

The device is currently mostly useless to me, so I am willing to experiment,
but I want to preserve a reliable recovery path.

PRIMARY GOAL

Build working OpenWrt firmware for my OP2200H with, ideally:

1. Ethernet/LAN
2. 2.4 GHz Wi-Fi
3. 5 GHz Wi-Fi
4. normal OpenWrt routing/LuCI
5. eventually GPON/OMCI
6. ideally hardware acceleration/offload later

I do NOT require the stock ISP firmware to remain the everyday firmware.

IMPORTANT RECOVERY STRATEGY

The router has two firmware slots.

Treat:

slot 0 = untouched recovery firmware
slot 1 = experimental firmware

Do NOT modify:
- Bismarck preloader
- U-Boot
- env
- env2
- static_conf
- slot 0

until there is an independently proven recovery reason to do so.

Initially all OpenWrt testing should be RAM-only using U-Boot TFTP + bootm.

Only after an OP2200H-specific RAM image works should persistent experimentation
be considered, and then only ubi_k1 / ubi_r1.

If slot 1 is broken:

power on
-> UART
-> interrupt U-Boot
-> run ub0

If Linux is completely broken but U-Boot remains intact, U-Boot supports TFTP
and bootm, so a rescue Linux/OpenWrt initramfs can be loaded entirely into RAM.

Do NOT execute commands such as:

saveenv
erase
nand write
ubi write
upk
upr
updev
upt
upv

unless there is a specific, reviewed reason.

=======================================================================
DEVICE
=======================================================================

Model:
OP2200H

ISP customization:
GTPL India

Current firmware:
GTPL_V4.0.1-241210

Router normal LAN IP:
192.168.1.1

PCB:
OVT_RTL9607C_SHELWG_V1.00

PCB date:
2023.09.15

=======================================================================
SOC / CPU
=======================================================================

SoC:
Realtek RTL9607C v2

Revision:
C

Chip ID:
0x96070001

Chip revision ID:
0x00000003

CPU:
dual-core MIPS interAptiv

Observed clocks:
CPU0 ~1150 MHz
CPU1 ~600 MHz

RAM:
256 MiB DDR3

DDR clock:
~666 MHz

=======================================================================
FLASH
=======================================================================

SPI NAND:
Winbond W25N01GVZEIG

Capacity:
1 Gbit / 128 MiB

JEDEC ID:
EF AA 21

Stock MTD layout observed from boot:

0x00000000 - 0x000c0000  boot
size 0x0c0000 = 768 KiB

0x000c0000 - 0x000e0000  env
size 0x020000 = 128 KiB

0x000e0000 - 0x00100000  env2
size 0x020000 = 128 KiB

0x00100000 - 0x00120000  static_conf
size 0x020000 = 128 KiB

0x00120000 - 0x07d80000  ubi_device

There appears to be roughly 2.5 MiB at the physical end of the NAND not
described by the stock kernel's command-line MTD layout. Treat it as
reserved/unknown, NOT as free space.

Kernel command line includes:

console=ttyS0,115200
ubi.mtd=4
root=31:7
rootfs=squashfs

=======================================================================
UBI / DUAL FIRMWARE
=======================================================================

UBI volumes observed:

ubi_Config
approximately 11,046,912 bytes

ubi_k0
10,539,008 bytes

ubi_r0
26,284,032 bytes

ubi_k1
10,539,008 bytes

ubi_r1
26,284,032 bytes

Slot 0:

kernel = ubi_k0
rootfs = ubi_r0
version = GTPL_V4.0.1-241210

Slot 1:

kernel = ubi_k1
rootfs = ubi_r1
version string begins approximately:
V4.0.1--e240304...

Slot 1 appears to be firmware dated around 2024-03-04.

It has been manually booted successfully from U-Boot and looks substantially
the same as slot 0.

Important U-Boot environment state previously observed:

sw_valid0=1
sw_valid1=1
sw_active=0
sw_commit=0
sw_tryactive=2

Normal boot remains slot 0.

Manual commands:

run ub0
boots slot 0

run ub1
boots slot 1 without making the selection persistent, assuming environment
is not subsequently saved.

Shared volume:
ubi_Config

Therefore both stock Linux slots may potentially change the same configuration.

Slot 1 kernel was manually read into RAM and checked with iminfo:

Legacy image
Image Name: Linux-4.4.140
Data Size: approximately 4,031,721 bytes
Load Address: 0x80010000
Entry Point: 0x808ef460
Checksum: OK

=======================================================================
BOOTLOADER
=======================================================================

Preloader:
Bismarck Preloader 3.7

Bootloader:
U-Boot 2020.01

Board banner:
RTL9607Cv2

Interactive prompt:
Phoebus#

Autoboot says:
Hit any key to stop autoboot:

U-Boot has working:
- tftp / tftpboot
- bootm
- ubi commands
- RAM reads
- iminfo

Previously observed network defaults approximately:

U-Boot ipaddr:
192.168.1.3

serverip:
192.168.1.7

Common RAM addresses seen:
0x81000000
freeAddr approximately 0x83000000

A RAM-only OpenWrt experiment should therefore use the conceptual flow:

TFTP initramfs into RAM
-> iminfo
-> bootm

and NOT write NAND.

=======================================================================
STOCK KERNEL
=======================================================================

Linux:
4.4.140

Toolchain:
Realtek MSDK GCC 4.8.5

Current stock build date:
2024-12-10

=======================================================================
WI-FI HARDWARE
=======================================================================

5 GHz:

Realtek RTL8812F / RTL8812FE / RTL8812FR family

PCI ID:
10ec:f812

Stock interface:
wlan0

PCIe:
port 0

PCIe reset GPIO:
40

2.4 GHz:

Realtek RTL8192F / RTL8192FE / RTL8192FR family

PCI ID:
10ec:818c

Stock interface:
wlan1

PCIe:
port 1

PCIe reset GPIO:
39

Stock Realtek Wi-Fi driver:

rtl8192cd

version:
4.0.8.4

date:
2022-07-07

stock driver git SHA:
b5606ad9229b4fb5867e3f7624535fcca2304a12

This exact two-radio combination matters greatly:

2.4 GHz = 10ec:818c
5 GHz   = 10ec:f812

=======================================================================
PON
=======================================================================

The board is an XPON/GPON HGU/ONT.

Optical driver/laser IC appears likely to be Semtech GN25L95-class.

The exact stock firmware demonstrably has a working Realtek PON/OMCI stack.

Eventually preserve/extract the stock device's ONU identity before doing
persistent GPON experiments:

- GPON serial
- PLOAM password
- vendor ID
- ONU model
- OMCI identifiers
- LOID and LOID password if applicable
- MAC addresses
- Realtek PON configuration

Do not casually change the ISP provisioning identity.

Do not modify TR-069/ACS/PON provisioning while merely testing router firmware.

=======================================================================
UART HARDWARE
=======================================================================

I am NOT buying another USB-UART adapter.

An ESP32 WROOM-32 is already working perfectly as the UART bridge.

Router UART header is labeled:

RX
TX
GND
VDD

Measured:
RX ~3.3 V
TX ~3.3 V idle
VDD ~3.3 V

UART:
115200 8N1

CURRENT ESP32 WIRING:

Router TX -> ESP32 GPIO23
Router RX <- ESP32 GPIO22
Router GND -> ESP32 GND
Router VDD -> DO NOT CONNECT

Router uses its normal PSU.
ESP32 uses USB from the Mac.

ESP32 bridge firmware concept:

void setup() {
    Serial.begin(115200);
    Serial2.begin(115200, SERIAL_8N1, 23, 22);
}

void loop() {
    while (Serial2.available())
        Serial.write(Serial2.read());

    while (Serial.available())
        Serial2.write(Serial.read());
}

The old GPIO17 recommendation is obsolete.
The current TX GPIO is GPIO22.

Mac serial device:

/dev/cu.usbserial-0001

Typical monitor:

screen -L /dev/cu.usbserial-0001 115200

Bidirectional UART has been proven:
- U-Boot interrupted successfully
- commands entered successfully
- slot 1 manually booted successfully

=======================================================================
BACKUP STATUS
=======================================================================

A full NAND backup has NOT been made.

This is an accepted tradeoff because the router is currently of little value
to me and slot 0 + U-Boot are being preserved as recovery.

I do have:
- boot logs
- printenv output
- flash/UBI layout information
- stock web config backup from previous work

The stock Linux runs a TFTP daemon on UDP 69, but attempting:

get etc/init.d/rc32 rc32

returned:
No such file or directory

Inspection of the corresponding Realtek GPL tftpd implementation suggests the
daemon is jailed/rooted to configured export directories, so it is not a simple
way to read /dev/mtd.

If a real flash dump becomes necessary, the preferred method is:

RAM-only rescue Linux/OpenWrt
-> read /dev/mtd* read-only
-> transfer dumps over Ethernet

Do not make backup requirements block the initial RAM-only OpenWrt bring-up.

=======================================================================
MAIN OPEN-SOURCE RESEARCH
=======================================================================

There are THREE important projects/resources.

-----------------------------------------------------------------------
1. alphaonex86/openwrt
-----------------------------------------------------------------------

Repository:

https://github.com/alphaonex86/openwrt

Use current main unless evidence shows otherwise.

This is now my preferred experimental base.

It contains a substantial reverse-engineered OpenWrt implementation for
Realtek PON SoCs.

Important target:

target/linux/realtek-luna/

It currently contains:
- rtl9607x
- rtl960x
- board DTS work
- Realtek Luna Ethernet work
- GPON work
- OMCI/common GPON infrastructure
- Wi-Fi work
- hardware/offload research

As of 2026-08-28, alphaonex86 has actively been consolidating the board
branches back into main.

Recent repository history states that the GPON implementation serves:
- RTL9602C
- RTL9603CVD
- RTL9607C

and includes separate RTL9607C GPON code such as:

CONFIG_RTL9607C_GPON
rtl9607c_gpon.o
rtl960x_ponmac.o

Important caveat:

target/linux/realtek-luna/image/rtl9607x.mk currently defines a generic:

Device/realtek_rtl9607c
DEVICE_DTS := rtl9607c_engboard

and describes it as an M1/headless serial + initramfs bring-up target.

So alphaonex86 DOES NOT currently provide a drop-in OP2200H image.

It is a source/driver base, not firmware we should blindly flash.

Very important Wi-Fi result:

alphaonex86 has working RTL8192FE support on the HSGQ X111W work.

His WAN-WIFI-STATUS.md records that RTL8192FE initially configured successfully
but did not radiate because the clean-room CARD-EMU->ACTIVE power sequence was
incomplete.

After adding the missing RTL8192F power sequence/RF initialization steps,
the AP radiated successfully across repeated scans/boots.

This is directly relevant because my 2.4 GHz radio is RTL8192F/RTL8192FE,
PCI ID 10ec:818c.

alphaonex86 also has extensive GPON/OMCI and hardware-offload work on other
Realtek platforms.

Do not assume the RTL9607F work is directly compatible with RTL9607C:
RTL9607F is a different SoC family/architecture.

-----------------------------------------------------------------------
2. jameywine/openwrt rtl9607c-dev
-----------------------------------------------------------------------

Repository:

https://github.com/jameywine/openwrt

Branch:

rtl9607c-dev

Related upstream OpenWrt PR:

openwrt/openwrt#20064

Title approximately:

realtek: add a new subtarget rtl9607c/rtl8198d with basic support

This is currently the strongest RTL9607C-specific OpenWrt platform work.

It includes/has worked on:

- RTL9607C / RTL8198D target support
- MIPS interAptiv
- clock
- thermal
- I2C
- SPI NAND
- Realtek NAND ECC
- PCIe
- initramfs booting
- BT-PON BT-G711AX RTL9607C board DTS
- U-Boot/TFTP RAM boot workflow

The PR explicitly describes the safe test procedure:

interrupt U-Boot
-> TFTP initramfs-kernel.bin
-> bootm

Persistent sysupgrade support is not considered mature/finished.

The currently defined reference target is largely:

BT-PON BT-G711AX

The BT-G711AX is very useful because it has:
- RTL9607C
- 256 MiB RAM
- 128 MiB SPI NAND
- exact Winbond W25N01GVZEIG
- U-Boot 2020.01

but its Wi-Fi hardware differs from the OP2200H.

Important PCIe/Wi-Fi discovery in jameywine's work:

a related RTL9607C board enumerates:

10ec:f812
and
10ec:818c

which are the EXACT PCI IDs in my OP2200H.

The 5-GHz RTL8812FE / f812 device has been made to work experimentally by
adapting the Linux rtw88 RTL8822C-family support.

RTL8192F / 818c was the weaker/unverified side in that work.

Therefore jameywine's tree is especially useful for:
- RTL9607C platform code
- PCIe
- Rev-C handling
- NAND/ECC
- 5-GHz RTL8812F work

-----------------------------------------------------------------------
3. jameywine/GPL-for-GP3000
-----------------------------------------------------------------------

Repository:

https://github.com/jameywine/GPL-for-GP3000

This is a Realtek GPL/vendor source dump using Linux 5.10.x.

Very important:

it contains:

drivers/net/wireless/realtek/rtl8192cd/

with explicit support for:

CONFIG_RTL_92F_SUPPORT
-> CONFIG_WLAN_HAL_8192FE

CONFIG_RTL_8812F_SUPPORT
-> CONFIG_WLAN_HAL_8812FE

and source/data trees including approximately:

WlanHAL/RTL88XX/RTL8812F/RTL8812FE/
WlanHAL/Data/8812F
WlanHAL/Data/8192F

This is the fallback if clean mac80211 drivers for both radios cannot be made
practical.

Possible Wi-Fi strategies:

CLEANER:

2.4 GHz:
port/fix proper RTL8192FE support, borrowing alphaonex86's working RTL8192FE
bring-up where applicable.

5 GHz:
adapt rtw88 RTL8812F work from jameywine.

PRACTICAL VENDOR FALLBACK:

port Realtek GPL rtl8192cd for:
- RTL8192FE
- RTL8812FE

The vendor driver is ugly compared with normal mac80211, but the stock router
already proves rtl8192cd supports both exact chips.

=======================================================================
ANIME4000 RTL960x COMMUNITY
=======================================================================

Useful knowledge base:

https://github.com/Anime4000/RTL960x

Important discussion:

https://github.com/Anime4000/RTL960x/discussions/474

In that discussion alphaonex86 reports approximately:

- RTL9607C working as headless server
- RTL9602C Ethernet verified
- RTL9602C Wi-Fi/GPON development
- later RTL9602C Ethernet + Wi-Fi + GPON working
- RTL9607F later ported with GPON + Ethernet + HW offload + Wi-Fi

He explicitly says board manufacturers combine chips differently, therefore
individual boards require customized DTS work.

This discussion is useful evidence/status, but Anime4000/RTL960x itself should
not be treated as the firmware base.

Use alphaonex86/openwrt for implementation.

=======================================================================
CLOSE HARDWARE RELATIVES
=======================================================================

Intelbras WiFiber 1200R:

- RTL9607C
- RTL8192FR
- RTL8812FR
- 2x Gigabit Ethernet

Known firmware:
ONT1200R_inMesh_2.2-240304.tar

The date 240304 matches suspiciously well with the OP2200H slot-1 version
string containing e240304.

This is an excellent reference/donor for reverse engineering.

DO NOT flash it directly onto OP2200H.

AZRoad AZ544G / AZ548G:

- RTL9607C-VB6
- RTL8192FR
- RTL8812FR
- GN25L95
- 128 MB NAND
- 256 MB DDR

Very close semiconductor stack.

Again:
reference only, do not blindly flash.

BT-PON BT-G711AX:

- RTL9607C
- W25N01GVZEIG
- 128 MiB NAND
- 256 MiB RAM
- U-Boot 2020.01

Excellent OpenWrt platform reference.
Wi-Fi differs.

=======================================================================
WHAT WE SHOULD BUILD
=======================================================================

Recommended architecture:

Start from:

alphaonex86/openwrt main

Create a dedicated OP2200H target/device.

Borrow/cherry-pick/adapt where useful from:

jameywine/openwrt rtl9607c-dev

especially:
- RTL9607C Rev-C support
- PCIe
- SPI NAND/ECC
- clock/platform pieces
- BT-G711AX DTS patterns
- RTL8812F / f812 rtw88 work

Use alpha's work for:
- Realtek Luna family structures
- Ethernet
- RTL8192FE knowledge/driver fixes
- GPON/OMCI family code
- eventual offload

Use GPL-for-GP3000 as:
- hardware documentation/oracle
- vendor driver reference
- possible rtl8192cd fallback

=======================================================================
OP2200H-SPECIFIC BOARD DEFINITION NEEDED
=======================================================================

Do NOT simply rename somebody else's DTS.

Create an OP2200H DTS/profile using confirmed hardware.

Known board-specific facts already available:

SoC = RTL9607C Rev C
RAM = 256 MiB
NAND = W25N01GVZEIG 128 MiB
UART = ttyS0 115200

2.4 radio:
PCI ID 10ec:818c
PCIe port 1
reset GPIO39

5 GHz radio:
PCI ID 10ec:f812
PCIe port 0
reset GPIO40

Stock MTD map:
boot        @ 0x000000 size 0x0c0000
env         @ 0x0c0000 size 0x020000
env2        @ 0x0e0000 size 0x020000
static_conf @ 0x100000 size 0x020000
ubi_device  @ 0x120000 through 0x07d80000

For initial bring-up, preferably mark flash partitions read-only in the DTS
or otherwise ensure the RAM image cannot accidentally modify them.

Unknown board details should be measured from the stock boot log / hardware,
not guessed from another board:
- exact Ethernet port numbering
- switch/PHY mapping
- LED GPIOs
- WPS/reset GPIOs
- optical control GPIOs
- Wi-Fi calibration source
- any board-specific PON/laser settings

=======================================================================
FIRST MILESTONES
=======================================================================

MILESTONE 1

Build an OP2200H-specific initramfs.

Absolutely no NAND install image needed.

TFTP to U-Boot RAM.

Conceptually:

Phoebus# tftp 0x81000000 <op2200h-initramfs-kernel.bin>
Phoebus# iminfo 0x81000000
Phoebus# bootm 0x81000000

Exact command/load address should be checked against the resulting image and
existing U-Boot environment before executing.

MILESTONE 2

Get serial OpenWrt shell.

Collect:

cat /proc/cpuinfo
cat /proc/mtd
cat /proc/meminfo
dmesg
ip link
lspci -nn
ls -l /sys/bus/pci/devices
mount

Verify NAND layout before doing ANY writable MTD operation.

MILESTONE 3

Ethernet.

At least one LAN port should become usable for SSH/ping.

MILESTONE 4

PCIe.

Must enumerate:

10ec:f812
10ec:818c

Verify the known reset GPIOs 40 and 39.

MILESTONE 5

2.4 GHz RTL8192F/FE.

Use alphaonex86's RTL8192FE work as the first reference.

MILESTONE 6

5 GHz RTL8812F/FE.

Use jameywine's f812 / rtw88 work as first reference.

MILESTONE 7

Only after stable RAM boot + Ethernet + recovery:
consider generating a persistent slot-1 image for:

ubi_k1
ubi_r1

NEVER overwrite slot0 during early development.

MILESTONE 8

GPON/OMCI.

Port/adapt alphaonex86's RTL9607C/Luna GPON work.

Before connecting experimental GPON firmware to the ISP network, preserve and
reproduce this particular ONU's actual identity rather than inventing values.

=======================================================================
WHAT I NEED FROM THIS CHAT
=======================================================================

Please act as the engineering/porting assistant for this specific OP2200H.

First:

1. inspect CURRENT alphaonex86/openwrt main, not an old snapshot;
2. inspect CURRENT jameywine/openwrt rtl9607c-dev;
3. compare the RTL9607C platform/DTS/PCIe/Ethernet/GPON/Wi-Fi implementations;
4. decide which source files to reuse rather than rewriting existing work;
5. create an OP2200H-specific board target;
6. initially build ONLY a RAM-bootable initramfs;
7. do not write NAND yet.

Whenever hardware assumptions are needed, ask for or derive a read-only
measurement instead of blindly copying another RTL9607C board.

The priority order is:

RAM boot
-> Ethernet
-> PCIe
-> 2.4 GHz
-> 5 GHz
-> stable router
-> slot-1 install
-> GPON/OMCI
-> hardware offload

The ESP32 UART bridge is already working and should not be redesigned.