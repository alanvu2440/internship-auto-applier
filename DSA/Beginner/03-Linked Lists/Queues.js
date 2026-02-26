class ListNode {
    constructor(val) {
        this.val = val;
        this.next = null; 
    }
}

class Queue {
    constructor() {
        // left is our head front, and right is our tail back
        this.left = null; 
        this.right = null;
    }

    // add to back the tail
    enqueue(val) {
        const newNode = new ListNode(val);
        if (this.right != null) {
            //if queue is not empty link tail to new node
            this.right.next = newNode;
            this.right = newNode;
        } else {
            // if queue is empty new node is head and tail 
            this.left = newNode;
            this.right = newNode;
        }
    }

    //remove from left the head
    dequeue() {
        if (this.left == null) {
            return null;
        }

        const val = this.left.val;
        this.left = this.left.next; 

        // critical fix: if queue is now empty reset right to null
        if (this.left == null) { 
            this.right = null
        }

        return val;
    }

    print() {
        let curr = this.left;
        let s = ""; 
        while (curr != null) {
            s += curr.val + "->  ";
            curr = curr.next
        }
        console.log(s + "null");
    }

}