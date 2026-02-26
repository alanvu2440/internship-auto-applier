class ListNode:
    def __init__(self, val): 
        self.val = val
        self.next = None
        self.prev = None

class DoublyLinkedList: 
    def __init__(self, val): 
        self.head = ListNode(-1)
        self.tail = ListNode(-1)
        self.head.next = self.tail 
        self.tail.prev = self.head

    def insertFront(self, val): 
        newNode = ListNode(val) 
        #connect to it's neighbors
        newNode.prev = self.head
        newNode.next = self.head.next

        #connect neighbors new node
        self.head.next.prev = newNode
        self.head.next = newNode

    def insertEnd(self, val):
        newNode = ListNode(val)

        #connect node to neighbors
        newNode.next = self.tail
        newNode.prev = self.tail.prev

        #neighbros to new node
        self.tail.prev.next = newNode
        self.tail.prev = newNode

    def removeFront(self):
        # Assume at least one node exists between dummies
        if self.head.next != self.tail: 
            self.head.next.next.prev = self.head
            self.head.next = self.head.next.next

    def removeEnd(self):
        if self.tail.prev != self.head:
            self.tail.prev.prev.next = self.tail
            self.tail.prev = self.tail.prev.prev

    def print_list(self):
        curr = self.head.next
        nodes =[]
        while curr != self.tail: 
            nodes.append(str(curr.val))
            curr = curr.next
        print(" <-> ".join(nodes))


