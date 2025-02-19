package ebpf

import (
	"encoding/binary"
	"fmt"
	"net"
	"sync"
	"syscall"
	"unsafe"

	"github.com/evilsocket/opensnitch/daemon/core"
	"github.com/evilsocket/opensnitch/daemon/log"
	daemonNetlink "github.com/evilsocket/opensnitch/daemon/netlink"
	"github.com/evilsocket/opensnitch/daemon/procmon"
	elf "github.com/iovisor/gobpf/elf"
)

//contains pointers to ebpf maps for a given protocol (tcp/udp/v6)
type ebpfMapsForProto struct {
	bpfmap *elf.Map
}

//Not in use, ~4usec faster lookup compared to m.LookupElement()

//mimics union bpf_attr's anonymous struct used by BPF_MAP_*_ELEM commands
//from <linux_headers>/include/uapi/linux/bpf.h
type bpf_lookup_elem_t struct {
	map_fd uint64 //even though in bpf.h its type is __u32, we must make it 8 bytes long
	//because "key" is of type __aligned_u64, i.e. "key" must be aligned on an 8-byte boundary
	key   uintptr
	value uintptr
}

type alreadyEstablishedConns struct {
	TCP   map[*daemonNetlink.Socket]int
	TCPv6 map[*daemonNetlink.Socket]int
	sync.RWMutex
}

var (
	m        *elf.Module
	lock     = sync.RWMutex{}
	mapSize  = uint(12000)
	ebpfMaps map[string]*ebpfMapsForProto
	//connections which were established at the time when opensnitch started
	alreadyEstablished = alreadyEstablishedConns{
		TCP:   make(map[*daemonNetlink.Socket]int),
		TCPv6: make(map[*daemonNetlink.Socket]int),
	}
	stopMonitors = make(chan bool)

	// list of local addresses of this machine
	localAddresses []net.IP

	hostByteOrder binary.ByteOrder
)

//Start installs ebpf kprobes
func Start() error {
	if err := mountDebugFS(); err != nil {
		log.Error("ebpf.Start -> mount debugfs error. Report on github please: %s", err)
		return err
	}

	m = elf.NewModule("/etc/opensnitchd/opensnitch.o")
	m.EnableOptionCompatProbe()

	if err := m.Load(nil); err != nil {
		log.Error("eBPF Failed to load /etc/opensnitchd/opensnitch.o: %v", err)
		return err
	}

	// if previous shutdown was unclean, then we must remove the dangling kprobe
	// and install it again (close the module and load it again)

	if err := m.EnableKprobes(0); err != nil {
		m.Close()
		if err := m.Load(nil); err != nil {
			log.Error("eBPF failed to load /etc/opensnitchd/opensnitch.o (2): %v", err)
			return err
		}
		if err := m.EnableKprobes(0); err != nil {
			log.Error("eBPF error when enabling kprobes: %v", err)
			return err
		}
	}
	determineHostByteOrder()

	ebpfCache = NewEbpfCache()
	ebpfMaps = map[string]*ebpfMapsForProto{
		"tcp": {
			bpfmap: m.Map("tcpMap")},
		"tcp6": {
			bpfmap: m.Map("tcpv6Map")},
		"udp": {
			bpfmap: m.Map("udpMap")},
		"udp6": {
			bpfmap: m.Map("udpv6Map")},
	}

	saveEstablishedConnections(uint8(syscall.AF_INET))
	if core.IPv6Enabled {
		saveEstablishedConnections(uint8(syscall.AF_INET6))
	}

	initEventsStreamer()

	go monitorCache()
	go monitorMaps()
	go monitorLocalAddresses()
	go monitorAlreadyEstablished()
	return nil
}

func saveEstablishedConnections(commDomain uint8) error {
	// save already established connections
	socketListTCP, err := daemonNetlink.SocketsDump(commDomain, uint8(syscall.IPPROTO_TCP))
	if err != nil {
		log.Debug("eBPF could not dump TCP (%d) sockets via netlink: %v", commDomain, err)
		return err
	}

	for _, sock := range socketListTCP {
		inode := int((*sock).INode)
		pid := procmon.GetPIDFromINode(inode, fmt.Sprint(inode,
			(*sock).ID.Source, (*sock).ID.SourcePort, (*sock).ID.Destination, (*sock).ID.DestinationPort))
		alreadyEstablished.Lock()
		alreadyEstablished.TCP[sock] = pid
		alreadyEstablished.Unlock()
	}
	return nil
}

// Stop stops monitoring connections using kprobes
func Stop() {
	for i := 0; i < 4; i++ {
		stopMonitors <- true
	}
	ebpfCache.clear()

	for i := 0; i < eventWorkers; i++ {
		stopStreamEvents <- true
	}

	if m != nil {
		m.Close()
	}

	for pm := range perfMapList {
		if pm != nil {
			pm.PollStop()
		}
	}
	for k, mod := range perfMapList {
		if mod != nil {
			mod.Close()
			delete(perfMapList, k)
		}
	}
}

//make bpf() syscall with bpf_lookup prepared by the caller
func makeBpfSyscall(bpf_lookup *bpf_lookup_elem_t) uintptr {
	BPF_MAP_LOOKUP_ELEM := 1 //cmd number
	syscall_BPF := 321       //syscall number
	sizeOfStruct := 40       //sizeof bpf_lookup_elem_t struct

	r1, _, _ := syscall.Syscall(uintptr(syscall_BPF), uintptr(BPF_MAP_LOOKUP_ELEM),
		uintptr(unsafe.Pointer(bpf_lookup)), uintptr(sizeOfStruct))
	return r1
}
